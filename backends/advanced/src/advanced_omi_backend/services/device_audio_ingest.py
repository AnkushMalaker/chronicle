"""Assemble timestamped ScreenPipe chunks into Chronicle conversation sessions."""

import asyncio
import hashlib
import logging
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from beanie import PydanticObjectId
from starlette.datastructures import UploadFile

from advanced_omi_backend.config import require_speech_for_transcription
from advanced_omi_backend.controllers.audio_controller import (
    upload_and_process_audio_files,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import DeviceInputItem, utcnow
from advanced_omi_backend.models.timeline import AudioEvidenceSpan
from advanced_omi_backend.models.user import User
from advanced_omi_backend.utils.vad_analysis import (
    AudioEvidenceProfile,
    SpeechDetectionReason,
    SpeechDetectionResult,
    profile_pcm_audio,
)

logger = logging.getLogger(__name__)

_SESSION_GAP = timedelta(seconds=60)
_CLOSE_DELAY = timedelta(seconds=90)
_MAX_SESSION = timedelta(minutes=30)
# Chunks the collector tagged with the same meeting interval belong to one
# conversation: tolerate longer silences and only split at a safety cap
# instead of the blind 30-minute rule.
_MEETING_SESSION_GAP = timedelta(minutes=5)
_MAX_MEETING_SESSION = timedelta(hours=2)


def _as_utc(value: datetime) -> datetime:
    """Normalize Mongo's naïve UTC datetimes before ordering or arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _meeting_id(item: DeviceInputItem) -> str | None:
    value = item.metadata.get("meeting_id")
    return str(value) if value else None


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
        # A meeting boundary always starts a new session; within one meeting
        # the collector's interval is trusted over the blind gap/duration
        # rules, up to a safety cap.
        same_meeting = _meeting_id(item) is not None and _meeting_id(
            item
        ) == _meeting_id(previous)
        gap_limit = _MEETING_SESSION_GAP if same_meeting else _SESSION_GAP
        max_session = _MAX_MEETING_SESSION if same_meeting else _MAX_SESSION
        if (
            _meeting_id(item) != _meeting_id(previous)
            or captured_at - previous_end > gap_limit
            or captured_at - session_start >= max_session
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


def _profile_wav(path: Path) -> AudioEvidenceProfile:
    """Decode and profile an assembled session once."""
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            pcm = handle.readframes(handle.getnframes())
    except Exception as error:
        detail = str(error).strip()
        detail = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
        logger.warning(
            "speech_gate_unscored reason=%s detail=%s path=%s",
            SpeechDetectionReason.WAV_DECODE_FAILED.value,
            detail,
            path,
        )
        return AudioEvidenceProfile(
            scored=False,
            reason=SpeechDetectionReason.WAV_DECODE_FAILED,
            bucket_seconds=10.0,
            speech_seconds=None,
            longest_no_speech_seconds=None,
            acoustic_active_seconds=0,
            acoustic_quiet_seconds=0,
            speech_fraction=[],
            acoustic_active_fraction=[],
            rms_dbfs=[],
            peak_dbfs=[],
            provider=None,
            frame_hop_ms=None,
        )
    return profile_pcm_audio(pcm, sample_rate, channels, sample_width)


def _speech_detection(profile: AudioEvidenceProfile) -> SpeechDetectionResult:
    if not profile.scored:
        return SpeechDetectionResult.unscored(profile.reason, profile.reason.value)
    if profile.reason == SpeechDetectionReason.NO_SPEECH:
        return SpeechDetectionResult.no_speech()
    return SpeechDetectionResult.speech()


def _coverage_profile(
    items: list[DeviceInputItem],
    started_at: datetime,
    ended_at: datetime,
    bucket_seconds: float,
) -> tuple[float, float, list[float]]:
    intervals = sorted(
        (
            max(started_at, _as_utc(item.captured_at)),
            min(ended_at, _as_utc(item.ended_at or item.captured_at)),
        )
        for item in items
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    covered = sum((end - start).total_seconds() for start, end in merged)
    duration = (ended_at - started_at).total_seconds()
    bucket_count = max(1, int((duration + bucket_seconds - 0.000001) // bucket_seconds))
    fractions: list[float] = []
    for index in range(bucket_count):
        bucket_start = started_at + timedelta(seconds=index * bucket_seconds)
        bucket_end = min(ended_at, bucket_start + timedelta(seconds=bucket_seconds))
        bucket_duration = (bucket_end - bucket_start).total_seconds()
        overlap = sum(
            max(0.0, (min(end, bucket_end) - max(start, bucket_start)).total_seconds())
            for start, end in merged
        )
        fractions.append(
            min(1.0, overlap / bucket_duration) if bucket_duration else 0.0
        )
    return covered, max(0.0, duration - covered), fractions


async def _save_evidence_span(
    session: list[DeviceInputItem],
    direction: str,
    profile: AudioEvidenceProfile,
    state: str,
    conversation_id: str | None = None,
) -> AudioEvidenceSpan:
    started_at = min(_as_utc(item.captured_at) for item in session)
    ended_at = max(_as_utc(item.ended_at or item.captured_at) for item in session)
    source_item_ids = [item.source_item_id for item in session]
    covered, missing, coverage = _coverage_profile(
        session, started_at, ended_at, profile.bucket_seconds
    )
    series_length = len(profile.acoustic_active_fraction)
    if len(coverage) < series_length:
        coverage.extend([0.0] * (series_length - len(coverage)))
    elif len(coverage) > series_length:
        coverage = coverage[:series_length]
    range_hash = hashlib.sha256("\n".join(source_item_ids).encode()).hexdigest()
    values = {
        "source_item_ids": source_item_ids,
        "source_range_hash": range_hash,
        "started_at": started_at,
        "ended_at": ended_at,
        "meeting_id": _meeting_id(session[0]),
        "conversation_id": conversation_id,
        "state": state,
        "covered_seconds": covered,
        "missing_seconds": missing,
        "bucket_seconds": profile.bucket_seconds,
        "coverage_fraction": coverage,
        "speech_fraction": profile.speech_fraction,
        "acoustic_active_fraction": profile.acoustic_active_fraction,
        "rms_dbfs": profile.rms_dbfs,
        "peak_dbfs": profile.peak_dbfs,
        "speech_seconds": profile.speech_seconds,
        "longest_no_speech_seconds": profile.longest_no_speech_seconds,
        "acoustic_active_seconds": profile.acoustic_active_seconds,
        "acoustic_quiet_seconds": profile.acoustic_quiet_seconds,
        "analysis": {
            "profile_version": "audio-evidence-v1",
            "vad_provider": profile.provider,
            "vad_reason": profile.reason.value,
            "vad_frame_hop_ms": profile.frame_hop_ms,
        },
    }
    existing = await AudioEvidenceSpan.find_one(
        AudioEvidenceSpan.user_id == session[0].user_id,
        AudioEvidenceSpan.source_id == session[0].source_id,
        AudioEvidenceSpan.direction == direction,
        AudioEvidenceSpan.first_source_item_id == source_item_ids[0],
        AudioEvidenceSpan.last_source_item_id == source_item_ids[-1],
    )
    if existing is not None:
        for field, value in values.items():
            setattr(existing, field, value)
        await existing.save()
        return existing
    span = AudioEvidenceSpan(
        user_id=session[0].user_id,
        source_id=session[0].source_id,
        first_source_item_id=source_item_ids[0],
        last_source_item_id=source_item_ids[-1],
        direction=direction if direction in {"input", "output"} else "unknown",
        **values,
    )
    await span.insert()
    return span


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
    rejected_no_speech = 0
    unscored_sessions = 0
    unscored_reasons: dict[str, int] = {}
    require_speech = require_speech_for_transcription()
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
                profile = _profile_wav(output)
                speech_detection = (
                    _speech_detection(profile) if require_speech else None
                )
                if speech_detection is not None and speech_detection.has_speech is None:
                    reason = speech_detection.reason.value
                    unscored_sessions += 1
                    unscored_reasons[reason] = unscored_reasons.get(reason, 0) + 1
                if speech_detection is not None and speech_detection.should_reject:
                    await _save_evidence_span(
                        session, direction, profile, state="no_speech"
                    )
                    # Silent session: never enters the conversation pipeline.
                    logger.info(
                        "🔇 ScreenPipe session %s-%s (%d chunks) has no speech "
                        "— rejecting without transcription (reason=%s, scored=%s)",
                        source_id,
                        direction,
                        len(session),
                        speech_detection.reason.value,
                        speech_detection.scored,
                    )
                    for item in session:
                        await item.delete()
                    rejected_no_speech += 1
                    continue
                with output.open("rb") as handle:
                    result = await upload_and_process_audio_files(
                        user,
                        [UploadFile(file=handle, filename=output.name)],
                        device_name=f"{source_id}-{direction}",
                        source="screenpipe",
                        external_source_id=(
                            f"screenpipe:{source_id}:{direction}:"
                            f"{session[0].source_item_id}-{session[-1].source_item_id}"
                        ),
                        external_source_type="screenpipe",
                        data_purpose="capture_evidence",
                        memory_excluded=True,
                        memory_exclusion_reason="continuous_screenpipe_capture",
                        skip_post_processing=True,
                    )
            if (
                not isinstance(result, dict)
                or not result.get("files")
                or result["files"][0].get("status") != "started"
            ):
                await _save_evidence_span(session, direction, profile, state="failed")
                continue
            conversation_id = result["files"][0]["conversation_id"]
            await _save_evidence_span(
                session,
                direction,
                profile,
                state="transcribed" if profile.scored else "unscored",
                conversation_id=conversation_id,
            )
            session_start = min(_as_utc(item.captured_at) for item in session)
            conversation = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if conversation is not None:
                conversation.created_at = session_start
                await conversation.save()
            observations = await DeviceInputItem.find(
                DeviceInputItem.user_id == user_id,
                DeviceInputItem.source_id == source_id,
                DeviceInputItem.kind == "observation",
                DeviceInputItem.captured_at <= session_end,
                {
                    "$or": [
                        {"ended_at": None},
                        {"ended_at": {"$gte": session_start}},
                    ]
                },
            ).to_list()
            for observation in observations:
                if conversation_id not in observation.related_conversation_ids:
                    observation.related_conversation_ids.append(conversation_id)
                    observation.related_conversation_ids.sort()
                    observation.curation = "pending"
                    await observation.save()
            for item in session:
                await item.delete()
            processed += 1
    return {
        "pending_chunks": len(pending),
        "processed_sessions": processed,
        "rejected_no_speech": rejected_no_speech,
        "vad_unscored_sessions": unscored_sessions,
        "vad_unscored_reasons": unscored_reasons,
    }
