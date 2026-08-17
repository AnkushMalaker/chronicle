"""
Audio-related RQ job functions.

This module contains jobs related to audio file processing and cropping.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from bson import Binary
from pymongo.errors import DuplicateKeyError
from rq import get_current_job

from advanced_omi_backend.models.audio_capture import (
    CAPTURE_CONTINUITY_TOLERANCE_SECONDS,
    AudioCaptureSession,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    PersistenceRuntimeState,
    ReadPhase,
    SessionPhase,
    parse_consumer_groups,
)
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
)
from advanced_omi_backend.utils.audio_chunk_utils import encode_pcm_to_opus
from advanced_omi_backend.utils.job_utils import check_job_alive

logger = logging.getLogger(__name__)


class AudioPersistenceError(RuntimeError):
    """A persistence attempt failed while Redis still owns the source messages."""


class AudioPersistenceInvariantError(AudioPersistenceError):
    """Capture identity/provenance cannot prove a safe Mongo commit."""


def _message_id_text(message_id: bytes | str) -> str:
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


def _captured_at(message_id: str) -> datetime | None:
    """Absolute capture time from a Redis stream ID.

    A stream ID is ``<milliseconds>-<sequence>``, stamped by Redis when the producer
    appended the audio. That is already the wall-clock time of the sound, so the
    streaming path needs no new plumbing to anchor its chunks — it just has to stop
    throwing the timestamp away.
    """
    milliseconds = message_id.split("-", 1)[0]
    if not milliseconds.isdigit():
        return None
    return datetime.fromtimestamp(int(milliseconds) / 1000.0, tz=timezone.utc)


def _captured_at_from_fields(fields: dict, message_id: str) -> datetime | None:
    raw = fields.get(b"captured_at")
    if raw is not None:
        try:
            value = raw.decode() if isinstance(raw, bytes) else str(raw)
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Invalid captured_at on Redis audio message %s", message_id)
    return _captured_at(message_id)


def _time_basis_from_fields(fields: dict) -> str:
    raw = fields.get(b"time_basis") or fields.get("time_basis")
    if raw is None:
        return "received"
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    if value not in {"recorded", "received"}:
        raise AudioPersistenceInvariantError(
            f"Invalid WAL capture time basis {value!r}"
        )
    return value


@async_job(redis=True, beanie=True)
async def audio_streaming_persistence_job(
    session_id: str,
    user_id: str,
    client_id: str,
    *,
    redis_client=None,
) -> Dict[str, Any]:
    """Commit a Redis raw-audio WAL to MongoDB with at-least-once delivery.

    Delivery state is strict: group lag -> pending -> Mongo commit -> XACK.

    A failed attempt raises. It never ACKs, trims, expires, or routes bytes through
    another store. RQ retries re-enter through pending recovery using the same
    deterministic consumer name.
    """
    audio_stream_name = f"audio:stream:{session_id}"
    audio_group_name = AUDIO_PERSISTENCE_GROUP
    audio_consumer_name = f"persistence-{session_id[-16:]}"
    logger.info(f"🎵 Starting durable Mongo audio persistence for session {session_id}")

    try:
        await redis_client.xgroup_create(
            audio_stream_name, audio_group_name, "0", mkstream=True
        )
    except Exception as error:
        if "BUSYGROUP" not in str(error):
            raise

    store = SessionStore(redis_client)
    sample_rate, channels, sample_width = await store.get_audio_format(session_id)
    bytes_per_second = sample_rate * sample_width * channels
    chunk_size_bytes = int(10.0 * bytes_per_second)

    runtime = PersistenceRuntimeState()
    pending_cursor: bytes | str = "0-0"
    start_time = time.time()
    max_runtime = 86340
    current_job = get_current_job()

    capture = await AudioCaptureSession.find_one(
        AudioCaptureSession.capture_session_id == session_id
    )
    if capture is None:
        raise AudioPersistenceInvariantError(
            f"Capture session {session_id} does not exist in MongoDB"
        )
    if capture.user_id != user_id or capture.client_id != client_id:
        raise AudioPersistenceInvariantError(
            f"Capture session {session_id} identity does not match persistence job"
        )

    last_chunk = (
        await AudioChunkDocument.find(
            AudioChunkDocument.capture_session_id == session_id
        )
        .sort("-sequence")
        .first_or_none()
    )
    next_sequence = last_chunk.sequence + 1 if last_chunk is not None else 0

    pcm_buffer = bytearray()
    pcm_message_ids: list[bytes | str] = []
    pcm_captured_at: datetime | None = None
    pcm_time_basis: str | None = None
    observed_time_basis: str | None = None
    total_pcm_bytes = 0
    total_compressed_bytes = 0
    total_mongo_chunks_written = 0
    end_signal_received = False

    async def find_existing_chunk(source_ids: list[str]):
        return await AudioChunkDocument.find_one(
            AudioChunkDocument.source_stream == audio_stream_name,
            AudioChunkDocument.source_first_message_id == source_ids[0],
        )

    def validate_existing_chunk(
        existing,
        source_ids: list[str],
        original_size: int,
    ) -> None:
        if existing.capture_session_id != session_id:
            raise AudioPersistenceInvariantError(
                f"Committed chunk {source_ids[0]} belongs to "
                f"capture {existing.capture_session_id}, not {session_id}"
            )
        if existing.user_id != user_id or existing.capture_source_id != client_id:
            raise AudioPersistenceInvariantError(
                f"Committed chunk {source_ids[0]} has different capture identity"
            )
        if list(existing.source_message_ids) != source_ids:
            raise AudioPersistenceInvariantError(
                "Redis replay grouped source messages differently from the committed "
                f"chunk beginning at {source_ids[0]}"
            )
        if existing.original_size != original_size:
            raise AudioPersistenceInvariantError(
                f"Committed chunk {source_ids[0]} has a different PCM size"
            )

    async def commit_and_ack_buffer() -> None:
        """Commit the local PCM buffer, then ACK exactly its source messages."""
        nonlocal pcm_buffer, pcm_message_ids, pcm_captured_at, pcm_time_basis
        nonlocal next_sequence
        nonlocal total_pcm_bytes, total_compressed_bytes, total_mongo_chunks_written

        if not pcm_buffer:
            return
        if not pcm_message_ids:
            raise AudioPersistenceInvariantError(
                "PCM buffer has no Redis source-message provenance"
            )

        source_ids = [_message_id_text(message_id) for message_id in pcm_message_ids]
        original_size = len(pcm_buffer)
        existing = await find_existing_chunk(source_ids)
        inserted = False

        if existing is None:
            captured_at = pcm_captured_at or _captured_at(source_ids[0])
            if captured_at is None:
                raise AudioPersistenceInvariantError(
                    f"WAL chunk {source_ids[0]} has no absolute capture time"
                )
            opus_data = await encode_pcm_to_opus(
                pcm_data=bytes(pcm_buffer),
                sample_rate=sample_rate,
                channels=channels,
                bitrate=24,
            )
            duration = original_size / bytes_per_second
            candidate = AudioChunkDocument(
                user_id=user_id,
                capture_source_id=client_id,
                capture_session_id=session_id,
                sequence=next_sequence,
                audio_data=Binary(opus_data),
                original_size=original_size,
                compressed_size=len(opus_data),
                duration=duration,
                captured_at=captured_at,
                sample_rate=sample_rate,
                channels=channels,
                source_stream=audio_stream_name,
                source_first_message_id=source_ids[0],
                source_last_message_id=source_ids[-1],
                source_message_ids=source_ids,
            )
            try:
                await candidate.insert()
                existing = candidate
                inserted = True
            except DuplicateKeyError:
                existing = await find_existing_chunk(source_ids)
                if existing is None:
                    raise

        validate_existing_chunk(existing, source_ids, original_size)

        await redis_client.xack(audio_stream_name, audio_group_name, *pcm_message_ids)

        if inserted:
            total_pcm_bytes += existing.original_size
            total_compressed_bytes += existing.compressed_size
            total_mongo_chunks_written += 1

        next_sequence = max(next_sequence, existing.sequence + 1)
        pcm_buffer = bytearray()
        pcm_message_ids = []
        pcm_captured_at = None
        pcm_time_basis = None

    async def process_messages(messages) -> bytes | str | None:
        """Move delivered entries to Mongo or leave them pending on any error."""
        nonlocal end_signal_received, pcm_captured_at, pcm_time_basis
        nonlocal observed_time_basis
        last_message_id = None
        for _stream_name, stream_messages in messages:
            for message_id, fields in stream_messages:
                last_message_id = message_id
                audio_data = (
                    fields.get(b"audio_data") or fields.get("audio_data") or b""
                )
                raw_chunk_id = fields.get(b"chunk_id") or fields.get("chunk_id") or b""
                chunk_id = (
                    raw_chunk_id.decode()
                    if isinstance(raw_chunk_id, bytes)
                    else str(raw_chunk_id)
                )
                is_terminal = chunk_id == "END" or bool(
                    fields.get(b"end_marker") or fields.get("end_marker")
                )
                raw_capture_id = fields.get(b"capture_session_id") or fields.get(
                    "capture_session_id"
                )
                if not raw_capture_id:
                    raise AudioPersistenceInvariantError(
                        f"Redis WAL entry {_message_id_text(message_id)} has no capture id"
                    )
                capture_id = (
                    raw_capture_id.decode()
                    if isinstance(raw_capture_id, bytes)
                    else str(raw_capture_id)
                )
                if capture_id != session_id:
                    raise AudioPersistenceInvariantError(
                        f"Redis WAL entry {_message_id_text(message_id)} belongs to "
                        f"capture {capture_id}, not {session_id}"
                    )

                if audio_data:
                    message_captured_at = _captured_at_from_fields(
                        fields, _message_id_text(message_id)
                    )
                    if message_captured_at is None:
                        raise AudioPersistenceInvariantError(
                            f"WAL chunk {_message_id_text(message_id)} has no absolute capture time"
                        )
                    message_time_basis = _time_basis_from_fields(fields)
                    if observed_time_basis is None:
                        observed_time_basis = message_time_basis
                        if capture.time_basis != message_time_basis:
                            # Persist this before the first audio document can become
                            # claimable. A field-level update avoids racing a later
                            # capture status/finalization update with a stale document.
                            await capture.set({"time_basis": message_time_basis})
                            capture.time_basis = message_time_basis
                    elif observed_time_basis != message_time_basis:
                        raise AudioPersistenceInvariantError(
                            f"Capture {session_id} mixes {observed_time_basis} and "
                            f"{message_time_basis} time bases"
                        )

                    if pcm_buffer:
                        if pcm_captured_at is None or pcm_time_basis is None:
                            raise AudioPersistenceInvariantError(
                                "Buffered PCM has no capture-time provenance"
                            )
                        expected_at = pcm_captured_at + timedelta(
                            seconds=len(pcm_buffer) / bytes_per_second
                        )
                        discontinuity_seconds = (
                            message_captured_at - expected_at
                        ).total_seconds()
                        if (
                            abs(discontinuity_seconds)
                            > CAPTURE_CONTINUITY_TOLERANCE_SECONDS
                        ):
                            logger.info(
                                "Splitting capture %s before WAL message %s at "
                                "%+.3fs timestamp discontinuity",
                                session_id,
                                _message_id_text(message_id),
                                discontinuity_seconds,
                            )
                            await commit_and_ack_buffer()

                    if not pcm_buffer:
                        pcm_captured_at = message_captured_at
                        pcm_time_basis = message_time_basis
                    pcm_buffer.extend(audio_data)
                    pcm_message_ids.append(message_id)
                    if len(pcm_buffer) >= chunk_size_bytes:
                        await commit_and_ack_buffer()
                else:
                    if is_terminal:
                        end_signal_received = True
                    await redis_client.xack(
                        audio_stream_name, audio_group_name, message_id
                    )
        return last_message_id

    async def persistence_group_drained() -> bool:
        raw_groups = await redis_client.execute_command(
            "XINFO", "GROUPS", audio_stream_name
        )
        progress = parse_consumer_groups(raw_groups or []).get(audio_group_name)
        return bool(progress and progress.drained)

    try:
        while True:
            if not await check_job_alive(redis_client, current_job, session_id):
                raise AudioPersistenceError(
                    f"RQ no longer owns persistence attempt for {session_id}"
                )
            if time.time() - start_time > max_runtime:
                raise AudioPersistenceError(
                    f"Persistence attempt for {session_id} reached its 24h handoff"
                )

            status = await store.get_status(session_id)
            if status in (SessionStatus.FINALIZING, SessionStatus.FINISHED):
                if runtime.session is SessionPhase.ACTIVE:
                    runtime.begin_draining()
                    logger.info(
                        f"🛑 Persistence draining terminal session {session_id}"
                    )
            elif status is not SessionStatus.ACTIVE:
                raise AudioPersistenceInvariantError(
                    f"Session {session_id} has invalid/missing status {status}"
                )

            read_cursor = (
                pending_cursor
                if runtime.reader is ReadPhase.RECOVERING_PENDING
                else ">"
            )
            messages = await redis_client.xreadgroup(
                audio_group_name,
                audio_consumer_name,
                {audio_stream_name: read_cursor},
                count=50,
                block=500,
            )

            # Redis returns [(stream, [])] (a truthy outer list) when a pending-ID
            # read has no entries for this consumer. Only a non-empty inner list is
            # delivery; otherwise pending recovery must advance to new entries.
            if any(stream_messages for _stream, stream_messages in messages):
                last_message_id = await process_messages(messages)
                if (
                    runtime.reader is ReadPhase.RECOVERING_PENDING
                    and last_message_id is not None
                ):
                    pending_cursor = last_message_id
                continue

            if runtime.reader is ReadPhase.RECOVERING_PENDING:
                runtime.pending_recovered()
                logger.info(
                    f"↩️ Pending recovery complete for persistence session {session_id}"
                )
                continue

            if runtime.session is SessionPhase.DRAINING:
                await commit_and_ack_buffer()
                if await persistence_group_drained():
                    runtime.complete()
                    break

    except Exception:
        runtime.fail()
        logger.exception(
            f"❌ Durable audio persistence attempt failed for session {session_id}; "
            "Redis entries remain unread/pending"
        )
        raise

    runtime_seconds = time.time() - start_time
    duration = total_pcm_bytes / bytes_per_second if total_pcm_bytes else 0.0
    compression_ratio = (
        total_compressed_bytes / total_pcm_bytes if total_pcm_bytes else 0.0
    )

    capture.status = "complete"
    if capture.ended_at is None:
        final_chunk = (
            await AudioChunkDocument.find(
                AudioChunkDocument.capture_session_id == session_id
            )
            .sort("-sequence")
            .first_or_none()
        )
        if final_chunk is not None:
            capture.ended_at = final_chunk.captured_at + timedelta(
                seconds=final_chunk.duration
            )
    await capture.save()

    await redis_client.delete(f"audio_persistence:session:{session_id}")
    logger.info(
        f"🎵 Durable audio persistence complete for session {session_id}: "
        f"{total_mongo_chunks_written} new Mongo chunks, "
        f"{total_pcm_bytes} PCM bytes, terminal_marker={end_signal_received}"
    )
    return {
        "session_id": session_id,
        "capture_session_id": session_id,
        "total_mongo_chunks": total_mongo_chunks_written,
        "total_pcm_bytes": total_pcm_bytes,
        "total_compressed_bytes": total_compressed_bytes,
        "compression_ratio": compression_ratio,
        "duration_seconds": duration,
        "runtime_seconds": runtime_seconds,
    }


# Enqueue wrapper functions
