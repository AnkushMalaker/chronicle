"""Conversation assignment lifecycle for a streaming audio session.

The Redis ``conversation:current`` value is an assignment: it tells persistence
which Mongo conversation owns the next audio chunk.  Creation and assignment must
therefore happen together, behind one module, rather than independently in the
conversation and persistence workers.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationAssignment:
    conversation_id: str
    created: bool


def _is_live_conversation(conversation: Conversation) -> bool:
    return (
        conversation.processing_status == Conversation.ConversationStatus.ACTIVE.value
        and not conversation.deleted
        and conversation.always_persist
        and not conversation.has_meaningful_transcript
    )


def _new_placeholder(user_id: str, client_id: str, session_id: str) -> Conversation:
    return Conversation(
        user_id=user_id,
        client_id=client_id,
        title="Audio Recording (Processing...)",
        summary="Transcription in progress...",
        transcript_versions=[],
        processing_status=Conversation.ConversationStatus.ACTIVE.value,
        always_persist=True,
        source_session_id=session_id,
    )


async def ensure_active_session_placeholder(
    store: SessionStore,
    *,
    session_id: str,
    user_id: str,
    client_id: str,
) -> Optional[ConversationAssignment]:
    """Return/create the audio placeholder for an active streaming session.

    The session status is checked inside the per-session creation lock. A
    finalizing/finished session never gets a new placeholder. Existing live
    assignments are reused; stale assignments are cleared before replacement.
    """
    async with store.conversation_create_lock(session_id) as acquired:
        if not acquired:
            logger.error(
                "Conversation creation lock timed out for session %s; "
                "continuing with serialized-state rechecks",
                session_id[:12],
            )

        if await store.get_status(session_id) != SessionStatus.ACTIVE:
            return None

        current_id = await store.get_current_conversation_id(session_id)
        if current_id:
            current = await Conversation.find_one(
                Conversation.conversation_id == current_id
            )
            if current and _is_live_conversation(current):
                return ConversationAssignment(current_id, created=False)

            await store.clear_current_conversation(session_id, expected_id=current_id)
            logger.warning(
                "Cleared stale conversation assignment %s for active session %s",
                current_id[:12],
                session_id[:12],
            )

        placeholder = _new_placeholder(user_id, client_id, session_id)
        await placeholder.insert()

        # Claim only if the session is still active and no concurrent creator won.
        # Session status + pointer are checked in one Redis transaction.
        claimed = await store.assign_current_conversation_if_active(
            session_id, placeholder.conversation_id, ttl=None
        )
        if not claimed:
            await placeholder.delete()

            # The claim can lose because either the session finalized or another
            # creator published first. Revalidate both facts before handing that
            # competing assignment to a caller that may restart workers from it.
            if await store.get_status(session_id) != SessionStatus.ACTIVE:
                return None

            current_id = await store.get_current_conversation_id(session_id)
            if not current_id:
                return None

            current = await Conversation.find_one(
                Conversation.conversation_id == current_id
            )
            if not current or not _is_live_conversation(current):
                return None

            return ConversationAssignment(current_id, created=False)

        logger.info(
            "Created placeholder conversation %s for active session %s",
            placeholder.conversation_id[:12],
            session_id[:12],
        )
        return ConversationAssignment(placeholder.conversation_id, created=True)


async def rotate_active_session_placeholder(
    store: SessionStore,
    *,
    session_id: str,
    expected_conversation_id: str,
    user_id: str,
    client_id: str,
) -> Optional[ConversationAssignment]:
    """Atomically replace a closing owner with a fresh durable placeholder.

    The replacement Mongo document is inserted first, then the Redis assignment is
    compare-and-swapped while the session is ACTIVE. Failure deletes the unclaimed
    candidate. At no point is an active producer given an ownerless gap.
    """
    async with store.conversation_create_lock(session_id) as acquired:
        if not acquired:
            logger.error(
                "Conversation rotation lock timed out for session %s",
                session_id[:12],
            )

        if await store.get_status(session_id) != SessionStatus.ACTIVE:
            return None

        current_id = await store.get_current_conversation_id(session_id)
        if current_id != expected_conversation_id:
            if not current_id:
                return None
            current = await Conversation.find_one(
                Conversation.conversation_id == current_id
            )
            if current and _is_live_conversation(current):
                return ConversationAssignment(current_id, created=False)
            return None

        placeholder = _new_placeholder(user_id, client_id, session_id)
        await placeholder.insert()
        claimed = await store.replace_current_conversation_if_active(
            session_id,
            expected_conversation_id,
            placeholder.conversation_id,
            ttl=None,
        )
        if not claimed:
            await placeholder.delete()
            return None

        logger.info(
            "Rotated active session %s from %s to placeholder %s",
            session_id[:12],
            expected_conversation_id[:12],
            placeholder.conversation_id[:12],
        )
        return ConversationAssignment(placeholder.conversation_id, created=True)
