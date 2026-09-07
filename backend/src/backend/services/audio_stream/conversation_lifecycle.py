"""Materialize semantic Conversations over durable capture evidence.

The capture producer and persistence worker never call this module. A detected
Conversation is created only after the speech gate fires, and a retry resolves to
the same document through its deterministic segmentation key.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from backend.constants import TITLE_NOT_GENERATED
from backend.models.audio_capture import AudioCaptureSession
from backend.models.conversation import Conversation, create_conversation


@dataclass(frozen=True)
class ConversationMaterialization:
    conversation: Conversation
    created: bool


async def materialize_detected_conversation(
    *,
    capture_session_id: str,
    user_id: str,
    client_id: str,
    speech_detected_at: float,
    pre_roll_seconds: float = 5.0,
    policy_revision: str = "v1",
) -> ConversationMaterialization:
    """Create the idempotent semantic claim shell for one detected interval.

    Audio ranges are attached when persistence catches up at finalization. Keeping
    that operation separate lets the UI show live speech without making the
    Conversation a prerequisite for durable audio.
    """
    capture = await AudioCaptureSession.find_one(
        AudioCaptureSession.capture_session_id == capture_session_id
    )
    if capture is None:
        raise RuntimeError(f"Capture session {capture_session_id} does not exist")
    if capture.user_id != user_id or capture.client_id != client_id:
        raise RuntimeError(
            f"Capture session {capture_session_id} does not match detected speech"
        )

    segmentation_key = (
        f"detected:{capture_session_id}:{int(speech_detected_at * 1000)}:"
        f"{policy_revision}"
    )
    existing = await Conversation.find_one(
        Conversation.segmentation_key == segmentation_key
    )
    if existing is not None:
        return ConversationMaterialization(existing, created=False)

    detected_at = datetime.fromtimestamp(speech_detected_at, tz=timezone.utc)
    started_at = max(
        capture.started_at,
        detected_at - timedelta(seconds=max(0.0, pre_roll_seconds)),
    )
    conversation = create_conversation(
        user_id=user_id,
        client_id=client_id,
        title=TITLE_NOT_GENERATED,
        summary="Transcribing audio...",
        origin="detected",
        started_at=started_at,
        segmentation_key=segmentation_key,
        memory_space_id=getattr(capture, "memory_space_id", None),
    )
    try:
        await conversation.insert()
        return ConversationMaterialization(conversation, created=True)
    except DuplicateKeyError:
        winner = await Conversation.find_one(
            Conversation.segmentation_key == segmentation_key
        )
        if winner is None:
            raise
        return ConversationMaterialization(winner, created=False)
