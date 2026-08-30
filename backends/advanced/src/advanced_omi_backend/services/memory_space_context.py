"""Explicit ScreenPipe evidence review for isolated-space note extraction."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import CaptureSource, DeviceInputJob
from advanced_omi_backend.services.manual_memories.config import manual_memory_settings
from advanced_omi_backend.services.vision import run_codex_vision

FRAMES_PER_REVIEW = 6
FRAME_WIDTH = 960

_VISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "screen_context": {"type": "string"},
        "visible_text": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["screen_context", "visible_text", "entities", "uncertainties"],
}

_VISION_PROMPT = """Describe only the information visible in these user-selected
screens that helps interpret the accompanying spoken transcript. Resolve references
such as 'this', 'what I am showing', diagrams, headings, product names, and lists.
Transcribe useful visible text faithfully. Do not infer private facts that are not
visible, and list uncertainty instead of guessing. The transcript remains the
authoritative account; the screens are supporting evidence selected by the user."""


def frame_key(source_id: str, frame_id: int) -> str:
    return f"{source_id}:{frame_id}"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def conversation_interval(conversation: Conversation) -> tuple[datetime, datetime]:
    start = _utc(conversation.started_at or conversation.created_at)
    end = conversation.ended_at
    if end is None:
        end = start + timedelta(seconds=conversation.audio_total_duration or 1)
    end = _utc(end)
    if end <= start:
        end = start + timedelta(seconds=max(conversation.audio_total_duration or 1, 1))
    return start, end


async def available_screen_sources(conversation: Conversation) -> list[CaptureSource]:
    return (
        await CaptureSource.find(
            {
                "user_id": conversation.user_id,
                "provider": "screenpipe",
                "capabilities": "screen_context",
            }
        )
        .sort("-last_seen_at")
        .to_list()
    )


async def request_contact_sheet(
    conversation: Conversation, source_id: str
) -> DeviceInputJob:
    source = await CaptureSource.find_one(
        {
            "user_id": conversation.user_id,
            "source_id": source_id,
            "provider": "screenpipe",
            "capabilities": "screen_context",
        }
    )
    if source is None:
        raise ValueError("Screen context source not found")

    existing = await DeviceInputJob.find_one(
        {
            "user_id": conversation.user_id,
            "source_id": source_id,
            "purpose": "memory_space_note_review",
            "payload.conversation_id": conversation.conversation_id,
            "status": {"$in": ["pending", "claimed"]},
        }
    )
    if existing is not None:
        return existing

    start, end = conversation_interval(conversation)
    job = DeviceInputJob(
        user_id=conversation.user_id,
        source_id=source_id,
        kind="thumbnail",
        start_at=start,
        end_at=end,
        purpose="memory_space_note_review",
        payload={
            "conversation_id": conversation.conversation_id,
            "memory_space_id": conversation.memory_space_id,
            "count": FRAMES_PER_REVIEW,
            "width": FRAME_WIDTH,
        },
    )
    await job.insert()
    conversation.memory_context_frames = [
        frame
        for frame in conversation.memory_context_frames
        if frame.source_id != source_id
    ]
    conversation.selected_memory_context_frame_keys = []
    conversation.memory_context_description = None
    conversation.memory_review_error = None
    conversation.memory_review_state = "context_requested"
    await conversation.save()
    return job


async def store_contact_sheet(
    job: DeviceInputJob, frames: Iterable[dict[str, Any]]
) -> Conversation:
    conversation_id = str(job.payload.get("conversation_id") or "")
    space_id = str(job.payload.get("memory_space_id") or "")
    conversation = await Conversation.find_one(
        {
            "conversation_id": conversation_id,
            "user_id": job.user_id,
            "memory_space_id": space_id,
        }
    )
    if conversation is None:
        raise ValueError("Memory-space recording not found")

    incoming = list(frames)[:FRAMES_PER_REVIEW]
    start, end = conversation_interval(conversation)
    span = end - start
    staged: list[Conversation.MemoryContextFrame] = []
    for index, frame in enumerate(sorted(incoming, key=lambda row: row["frame_id"])):
        # ScreenPipe resolves interval samples on the node. Its upload contract names
        # the exact frame but not its timestamp, so place it at the centre of the slice
        # used to select it. This is honest UI provenance, not an invented exact time.
        captured_at = start + span * ((index + 0.5) / max(len(incoming), 1))
        staged.append(
            Conversation.MemoryContextFrame(
                source_id=job.source_id,
                frame_id=int(frame["frame_id"]),
                captured_at=captured_at,
                content_type=str(frame.get("content_type") or "image/jpeg"),
                data=frame["data"],
            )
        )
    conversation.memory_context_frames = [
        frame
        for frame in conversation.memory_context_frames
        if frame.source_id != job.source_id
    ] + staged
    conversation.memory_review_state = "ready" if staged else "awaiting_context"
    conversation.memory_review_error = None if staged else "No frames were available"
    await conversation.save()
    return conversation


def selected_frames(
    conversation: Conversation,
) -> list[Conversation.MemoryContextFrame]:
    selected = set(conversation.selected_memory_context_frame_keys)
    return [
        frame
        for frame in conversation.memory_context_frames
        if frame_key(frame.source_id, frame.frame_id) in selected
    ]


async def describe_selected_frames(conversation: Conversation) -> str:
    frames = selected_frames(conversation)
    if not frames:
        return ""
    transcript = (conversation.transcript or "").strip()
    context = {
        "conversation_id": conversation.conversation_id,
        "captured_at": conversation_interval(conversation)[0].isoformat(),
        "transcript": transcript,
        "frames": [
            {
                "filename": f"frame-{frame.frame_id}.jpg",
                "source_id": frame.source_id,
                "approximate_captured_at": (
                    frame.captured_at.isoformat() if frame.captured_at else None
                ),
            }
            for frame in frames
        ],
    }
    result = await run_codex_vision(
        f"{_VISION_PROMPT}\n\nRecording:\n{json.dumps(context, ensure_ascii=False)}",
        [(f"frame-{frame.frame_id}.jpg", frame.data) for frame in frames],
        _VISION_SCHEMA,
        manual_memory_settings().codex,
    )
    lines = [str(result["screen_context"]).strip()]
    visible = [
        str(item).strip() for item in result["visible_text"] if str(item).strip()
    ]
    entities = [str(item).strip() for item in result["entities"] if str(item).strip()]
    uncertainties = [
        str(item).strip() for item in result["uncertainties"] if str(item).strip()
    ]
    if visible:
        lines.append("Visible text: " + " | ".join(visible))
    if entities:
        lines.append("Visible entities: " + ", ".join(entities))
    if uncertainties:
        lines.append("Uncertain visual details: " + " | ".join(uncertainties))
    return "\n".join(line for line in lines if line)
