from datetime import datetime, timedelta, timezone

import pytest

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import DeviceInputJob
from advanced_omi_backend.services.memory_space_context import (
    FRAMES_PER_REVIEW,
    conversation_interval,
    frame_key,
    selected_frames,
    store_contact_sheet,
)

pytestmark = pytest.mark.unit


def _conversation(**overrides):
    start = datetime(2026, 8, 30, 5, 15, tzinfo=timezone.utc)
    values = {
        "conversation_id": "conversation-1",
        "user_id": "user-1",
        "client_id": "web",
        "memory_space_id": "9f3523c8-af75-469d-995a-7179531f3fc8",
        "started_at": start,
        "created_at": start,
        "ended_at": start + timedelta(minutes=3),
        "memory_review_state": "awaiting_context",
        "memory_context_frames": [],
        "selected_memory_context_frame_keys": [],
    }
    values.update(overrides)
    return Conversation.model_construct(**values)


def test_review_uses_exact_recording_interval():
    conversation = _conversation()
    assert conversation_interval(conversation) == (
        conversation.started_at,
        conversation.ended_at,
    )


@pytest.mark.asyncio
async def test_returned_frames_are_bounded_and_only_explicit_selection_is_admitted(
    monkeypatch,
):
    conversation = _conversation(memory_review_state="context_requested")
    job = DeviceInputJob.model_construct(
        user_id="user-1",
        source_id="screenpipe-rainbow",
        purpose="memory_space_note_review",
        payload={
            "conversation_id": conversation.conversation_id,
            "memory_space_id": conversation.memory_space_id,
        },
    )

    async def find_one(*_args, **_kwargs):
        return conversation

    async def save_conversation(self, *_args, **_kwargs):
        return self

    monkeypatch.setattr(Conversation, "find_one", find_one)
    monkeypatch.setattr(Conversation, "save", save_conversation)

    await store_contact_sheet(
        job,
        [
            {
                "frame_id": index,
                "data": f"pixel-{index}".encode(),
                "content_type": "image/jpeg",
            }
            for index in range(10)
        ],
    )
    assert len(conversation.memory_context_frames) == FRAMES_PER_REVIEW
    assert conversation.memory_review_state == "ready"

    chosen = conversation.memory_context_frames[2]
    conversation.selected_memory_context_frame_keys = [
        frame_key(chosen.source_id, chosen.frame_id)
    ]
    assert [
        (frame.frame_id, frame.data) for frame in selected_frames(conversation)
    ] == [(chosen.frame_id, chosen.data)]
