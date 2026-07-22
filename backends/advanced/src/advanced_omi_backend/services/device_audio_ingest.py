"""Assemble timestamped ScreenPipe chunks into Chronicle conversation sessions."""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from beanie import PydanticObjectId
from starlette.datastructures import UploadFile

from advanced_omi_backend.controllers.audio_controller import (
    upload_and_process_audio_files,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import DeviceInputItem, utcnow
from advanced_omi_backend.models.user import User

_SESSION_GAP = timedelta(seconds=60)
_CLOSE_DELAY = timedelta(seconds=90)
_MAX_SESSION = timedelta(minutes=30)


def _as_utc(value: datetime) -> datetime:
    """Normalize Mongo's naïve UTC datetimes before ordering or arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def group_audio_sessions(items: list[DeviceInputItem]) -> list[list[DeviceInputItem]]:
    sessions: list[list[DeviceInputItem]] = []
    for item in sorted(items, key=lambda row: _as_utc(row.captured_at)):
        if not sessions:
            sessions.append([item])
            continue
        previous = sessions[-1][-1]
        previous_end = _as_utc(previous.ended_at or previous.captured_at)
        session_start = _as_utc(sessions[-1][0].captured_at)
        captured_at = _as_utc(item.captured_at)
        if (
            captured_at - previous_end > _SESSION_GAP
            or captured_at - session_start >= _MAX_SESSION
        ):
            sessions.append([item])
        else:
            sessions[-1].append(item)
    return sessions


def audio_stream_key(item: DeviceInputItem) -> tuple[str, str, str]:
    """Keep microphone and system output in independent processing streams."""
    return (
        item.user_id,
        item.source_id,
        str(item.metadata.get("direction", "unknown")),
    )


async def _mix_session(
    items: list[DeviceInputItem], workspace: Path, output: Path
) -> None:
    start = min(_as_utc(item.captured_at) for item in items)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    valid = [item for item in items if item.media_data]
    if not valid:
        raise ValueError("session has no available audio data")
    for index, item in enumerate(valid):
        suffix = Path(item.media_filename or "chunk.wav").suffix or ".wav"
        input_path = workspace / f"input-{index}{suffix}"
        input_path.write_bytes(item.media_data or b"")
        command.extend(["-i", str(input_path)])
    chains = []
    labels = []
    for index, item in enumerate(valid):
        delay_ms = max(
            0, int((_as_utc(item.captured_at) - start).total_seconds() * 1000)
        )
        label = f"a{index}"
        chains.append(
            f"[{index}:a]aresample=16000,aformat=channel_layouts=mono,adelay={delay_ms}[{label}]"
        )
        labels.append(f"[{label}]")
    chains.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=1,alimiter=limit=0.95[out]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[out]",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio assembly failed: {stderr.decode(errors='replace')[-1000:]}"
        )


async def process_device_audio() -> dict[str, Any]:
    pending = (
        await DeviceInputItem.find(
            DeviceInputItem.kind == "audio",
            DeviceInputItem.state == "received",
        )
        .sort([("source_id", 1), ("captured_at", 1)])
        .to_list()
    )
    by_source: dict[tuple[str, str, str], list[DeviceInputItem]] = {}
    for item in pending:
        by_source.setdefault(audio_stream_key(item), []).append(item)
    processed = 0
    for (user_id, source_id, direction), source_items in by_source.items():
        try:
            user = await User.get(PydanticObjectId(user_id))
        except Exception:
            user = None
        if user is None:
            continue
        for session in group_audio_sessions(source_items):
            session_end = max(
                _as_utc(item.ended_at or item.captured_at) for item in session
            )
            if session_end > utcnow() - _CLOSE_DELAY:
                continue
            with tempfile.TemporaryDirectory(
                prefix="chronicle-screenpipe-"
            ) as temp_dir:
                output = (
                    Path(temp_dir)
                    / f"screenpipe-{source_id}-{session[0].source_item_id}.wav"
                )
                await _mix_session(session, Path(temp_dir), output)
                with output.open("rb") as handle:
                    result = await upload_and_process_audio_files(
                        user,
                        [UploadFile(file=handle, filename=output.name)],
                        device_name=f"{source_id}-{direction}",
                        source="screenpipe",
                    )
            if (
                not isinstance(result, dict)
                or not result.get("files")
                or result["files"][0].get("status") != "started"
            ):
                continue
            conversation_id = result["files"][0]["conversation_id"]
            conversation = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if conversation is not None:
                conversation.created_at = min(
                    _as_utc(item.captured_at) for item in session
                )
                conversation.external_source_id = f"screenpipe:{source_id}:{direction}:{session[0].source_item_id}-{session[-1].source_item_id}"
                conversation.external_source_type = "screenpipe"
                await conversation.save()
            for item in session:
                item.state = "linked"
                item.conversation_id = conversation_id
                await item.save()
                item.media_data = None
                await item.save()
            processed += 1
    return {"pending_chunks": len(pending), "processed_sessions": processed}
