"""Classification-gated dispatch of user-facing conversation events.

Under rolling reconciliation a recording closing is a scheduling signal, not proof
that a conversation happened. The close path still runs every evidence producer and
still writes the provisional Conversation source record, but ``conversation.complete``
and the per-conversation consolidated memory write now fire from the **first settled
conversational Episode revision**, latched per ``(episode_key, event_type)`` so
resettlement or supersession never re-fires them.

See docs/backend/rolling-reconciliation.md, "Classification-gated event dispatch".

Failure semantics: the latch is claimed *before* the side effects, so two concurrent
publishes cannot both dispatch. If dispatch then raises, the latch is deleted again so
a later settlement re-fires it. That makes delivery at-least-once rather than
at-most-once, which is the right trade here: the downstream work (plugin events, the
memory write keyed on the conversation) is idempotent or re-runnable, while a silently
dropped dispatch means an episode the user never hears about and no record of why.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.timeline import (
    EpisodeDispatchLatch,
    TimelineEpisode,
    utcnow,
)
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services.plugin_service import dispatch_plugin_event
from advanced_omi_backend.services.sse_publisher import publish_sse_event

logger = logging.getLogger(__name__)

CONVERSATION_COMPLETE = PluginEvent.CONVERSATION_COMPLETE.value


_users_col = None


def _get_users_col():
    global _users_col
    if _users_col is None:
        # Lazy import: standalone sync pymongo client, deliberately not Beanie/Motor —
        # same constraint as workers/job_callbacks.py (see below).
        from pymongo import MongoClient

        uri = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
        db_name = os.getenv("MONGODB_DATABASE", "chronicle")
        _users_col = MongoClient(uri, serverSelectionTimeoutMS=5000)[db_name]["users"]
    return _users_col


def active_pipeline_sync(user_id: str) -> str:
    """Read ``User.active_timeline_pipeline`` from synchronous code.

    ``start_post_conversation_jobs`` is sync and runs inside a running event loop, so
    it can neither await Beanie nor block on it. This is one projected ``_id`` lookup
    per conversation close, through a standalone sync client for the same reason
    ``workers/job_callbacks.py`` uses one: the caller's process may not have Beanie
    initialized. Any failure answers ``"day"`` — the unchanged existing behaviour.
    """

    try:
        document = _get_users_col().find_one(
            {"_id": ObjectId(user_id)}, {"active_timeline_pipeline": 1}
        )
    except Exception:
        logger.warning(
            "Could not read the active timeline pipeline for %s; assuming 'day'",
            user_id,
            exc_info=True,
        )
        return "day"
    if not document:
        return "day"
    return document.get("active_timeline_pipeline") or "day"


async def _related_conversations(episode: TimelineEpisode) -> list[Conversation]:
    ids = [item for item in episode.related_conversation_ids if item]
    if not ids:
        return []
    return await Conversation.find({"conversation_id": {"$in": ids}}).to_list()


async def _fire(episode: TimelineEpisode, conversations: list[Conversation]) -> None:
    """Run exactly the work the close path used to run, with reconciled bounds."""

    # Imported here to break the import cycle: workers.memory_jobs imports the queue
    # controller, which imports the timeline dirty-range trigger from this package.
    from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing

    transcript = "\n".join(
        conversation.transcript
        for conversation in conversations
        if conversation.transcript
    )
    await dispatch_plugin_event(
        event=PluginEvent.CONVERSATION_COMPLETE,
        user_id=episode.user_id,
        data={
            "conversation": {
                "client_id": "",
                "user_id": episode.user_id,
            },
            "transcript": transcript,
            "duration": (episode.ended_at - episode.started_at).total_seconds(),
            "conversation_id": (
                episode.related_conversation_ids[0]
                if episode.related_conversation_ids
                else episode.episode_id
            ),
            "episode_key": episode.episode_key,
            "episode_id": episode.episode_id,
            "started_at": episode.started_at,
            "ended_at": episode.ended_at,
            "title": episode.title,
            "summary": episode.summary,
            "related_conversation_ids": list(episode.related_conversation_ids),
        },
        metadata={
            "source": "timeline_episode",
            "episode_key": episode.episode_key,
            "revision": int(episode.revision),
        },
        description=(
            f"episode={episode.episode_key[:12]}, revision={episode.revision}, "
            f"conversations={len(conversations)}"
        ),
        require_router=True,
    )

    for conversation in conversations:
        # The memory write is per conversation, but it is now caused by the episode
        # settling rather than by the recording closing.
        enqueue_memory_processing(conversation.conversation_id)


async def _retype_media_source_record(episode: TimelineEpisode) -> None:
    """A media/noise episode must not leave a false conversation note behind.

    TODO(WP-B): the vault mutation belongs to the typed write orchestration that owns
    ``Conversations/<id>.md``. ``services/memory/conversation_note.py`` only renders a
    note into a path a writer already chose; retyping means locating the existing note
    in the active vault policy, rewriting its category, and auditing the change. Doing
    that from here would invent a second vault writer, so this records the decision and
    leaves the mutation to the vault-projection package.
    """

    logger.info(
        "🩹 Settled non-conversational episode %s (kind=%s) dispatches nothing; "
        "source record retype deferred to the vault projection (WP-B)",
        episode.episode_key,
        episode.kind,
    )


async def dispatch_settled_episodes(user_id: str, episode_ids: list[str]) -> list[str]:
    """Fire user-facing events for newly settled conversational episodes.

    Returns the ``episode_key``s dispatched by this call (empty when every candidate
    was already latched, unsettled, or non-conversational).
    """

    if not episode_ids:
        return []

    episodes = await TimelineEpisode.find(
        {"episode_id": {"$in": list(episode_ids)}, "user_id": user_id}
    ).to_list()

    dispatched: list[str] = []
    for episode in episodes:
        if episode.status != "settled":
            continue
        if not episode.conversational:
            await _retype_media_source_record(episode)
            continue

        latch = EpisodeDispatchLatch(
            user_id=user_id,
            episode_key=episode.episode_key,
            event_type=CONVERSATION_COMPLETE,
            episode_id=episode.episode_id,
            revision=int(episode.revision),
        )
        try:
            await latch.insert()
        except DuplicateKeyError:
            logger.debug(
                "🩹 %s already dispatched for episode %s",
                CONVERSATION_COMPLETE,
                episode.episode_key,
            )
            continue

        try:
            await _fire(episode, await _related_conversations(episode))
        except Exception:
            # Release the claim so a later settlement re-fires. See module docstring.
            await EpisodeDispatchLatch.find(
                EpisodeDispatchLatch.episode_key == episode.episode_key,
                EpisodeDispatchLatch.event_type == CONVERSATION_COMPLETE,
            ).delete()
            logger.error(
                "❌ Dispatch of %s failed for episode %s; latch released for retry",
                CONVERSATION_COMPLETE,
                episode.episode_key,
                exc_info=True,
            )
            continue

        dispatched.append(episode.episode_key)
        logger.info(
            "📌 Dispatched %s for settled episode %s (revision %s)",
            CONVERSATION_COMPLETE,
            episode.episode_key,
            episode.revision,
        )

    return dispatched


@async_job(redis=True, beanie=True)
async def finalize_conversation_close_job(
    conversation_id: str,
    client_id: str,
    user_id: str,
    end_reason: Optional[str] = None,
    trigger: str = "",
    *,
    redis_client=None,
) -> dict[str, Any]:
    """Terminal close-path job for rolling users: settle state, dispatch nothing.

    ``dispatch_conversation_complete_event_job`` does two separable things: it owns the
    conversation's final ``end_reason``/``completed_at``/``processing_status``, and it
    fires ``conversation.complete``. Under rolling reconciliation only the first still
    belongs to the close path. This job is that half; it disappears when the day
    pipeline is removed at cutover and the remaining finalizer stops dispatching.
    """

    # Imported here to break the import cycle with the workers package, which imports
    # the queue controller and hence this package's dirty-range trigger.
    from advanced_omi_backend.workers.conversation_jobs import (
        request_conversation_context_jobs,
    )

    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id
    )
    if not conversation:
        logger.error(f"Conversation {conversation_id} not found")
        return {"success": False, "error": "Conversation not found"}

    needs_save = False
    if end_reason and conversation.end_reason is None:
        try:
            conversation.end_reason = Conversation.EndReason(end_reason)
        except ValueError:
            logger.error(
                "⚠️ %s is not a Conversation.EndReason (conversation %s); "
                "storing UNKNOWN",
                end_reason,
                conversation_id,
            )
            conversation.end_reason = Conversation.EndReason.UNKNOWN
        needs_save = True

    if conversation.completed_at is None:
        conversation.completed_at = utcnow()
        needs_save = True

    if conversation.apply_status(settled=True):
        needs_save = True

    if needs_save:
        await conversation.save()

    try:
        await request_conversation_context_jobs(conversation)
    except Exception as exc:
        logger.warning(
            "Failed to request device context for conversation %s: %s",
            conversation_id,
            exc,
        )

    publish_sse_event(
        user_id,
        "conversation.completed",
        {
            "conversation_id": conversation_id,
            "end_reason": (
                conversation.end_reason.value if conversation.end_reason else None
            ),
            "trigger": trigger,
        },
    )
    logger.info(
        "🏁 Finalized conversation %s status=%s (rolling: events fire on episode "
        "settlement)",
        conversation_id[:12],
        conversation.processing_status,
    )
    return {"success": True, "conversation_id": conversation_id, "dispatched": False}
