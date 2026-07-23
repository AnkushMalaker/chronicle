"""
Audio-related RQ job functions.

This module contains jobs related to audio file processing and cropping.
"""

import logging
import time
from typing import Any, Dict

from bson import Binary
from pymongo.errors import DuplicateKeyError
from rq import get_current_job

from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
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
from advanced_omi_backend.utils.audio_chunk_utils import (
    encode_pcm_to_opus,
    get_resume_position,
)
from advanced_omi_backend.utils.job_utils import check_job_alive

logger = logging.getLogger(__name__)


class AudioPersistenceError(RuntimeError):
    """A persistence attempt failed while Redis still owns the source messages."""


class AudioPersistenceInvariantError(AudioPersistenceError):
    """The configured lifecycle cannot provide a durable owner for raw audio."""


def _message_id_text(message_id: bytes | str) -> str:
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


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
    seen_conversations: set[str] = set()
    conversation_positions: dict[str, tuple[int, float]] = {}

    pcm_buffer = bytearray()
    pcm_message_ids: list[bytes | str] = []
    pcm_conversation_id: str | None = None
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
        conversation_id: str,
    ) -> None:
        if existing.conversation_id != conversation_id:
            raise AudioPersistenceInvariantError(
                f"Committed chunk {source_ids[0]} belongs to "
                f"{existing.conversation_id}, not WAL owner {conversation_id}"
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
        nonlocal pcm_buffer, pcm_message_ids, pcm_conversation_id
        nonlocal total_pcm_bytes, total_compressed_bytes, total_mongo_chunks_written

        if not pcm_buffer:
            return
        if not pcm_conversation_id:
            raise AudioPersistenceInvariantError(
                "PCM reached persistence without a conversation owner"
            )
        if not pcm_message_ids:
            raise AudioPersistenceInvariantError(
                "PCM buffer has no Redis source-message provenance"
            )

        source_ids = [_message_id_text(message_id) for message_id in pcm_message_ids]
        original_size = len(pcm_buffer)
        chunk_index, chunk_start_time = conversation_positions.get(
            pcm_conversation_id, (None, None)
        )
        if chunk_index is None or chunk_start_time is None:
            chunk_index, chunk_start_time = await get_resume_position(
                pcm_conversation_id
            )
        existing = await find_existing_chunk(source_ids)
        inserted = False

        if existing is None:
            opus_data = await encode_pcm_to_opus(
                pcm_data=bytes(pcm_buffer),
                sample_rate=sample_rate,
                channels=channels,
                bitrate=24,
            )
            duration = original_size / bytes_per_second
            end_time = chunk_start_time + duration
            candidate = AudioChunkDocument(
                conversation_id=pcm_conversation_id,
                chunk_index=chunk_index,
                audio_data=Binary(opus_data),
                original_size=original_size,
                compressed_size=len(opus_data),
                start_time=chunk_start_time,
                end_time=end_time,
                duration=duration,
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

        validate_existing_chunk(
            existing, source_ids, original_size, pcm_conversation_id
        )

        conversation = await Conversation.find_one(
            Conversation.conversation_id == existing.conversation_id
        )
        if conversation is None:
            raise AudioPersistenceInvariantError(
                f"WAL owner {existing.conversation_id} does not exist in MongoDB"
            )
        if conversation.source_session_id != session_id:
            raise AudioPersistenceInvariantError(
                f"WAL owner {existing.conversation_id} belongs to session "
                f"{conversation.source_session_id}, not {session_id}"
            )
        conversation.audio_chunks_count = max(
            conversation.audio_chunks_count or 0, existing.chunk_index + 1
        )
        conversation.audio_total_duration = max(
            conversation.audio_total_duration or 0.0, existing.end_time
        )
        conversation.audio_compression_ratio = (
            existing.compressed_size / existing.original_size
        )
        await conversation.save()

        await redis_client.xack(audio_stream_name, audio_group_name, *pcm_message_ids)

        if inserted:
            total_pcm_bytes += existing.original_size
            total_compressed_bytes += existing.compressed_size
            total_mongo_chunks_written += 1

        conversation_positions[pcm_conversation_id] = (
            max(chunk_index, existing.chunk_index + 1),
            max(chunk_start_time, existing.end_time),
        )
        pcm_buffer = bytearray()
        pcm_message_ids = []
        pcm_conversation_id = None

    async def process_messages(messages) -> bytes | str | None:
        """Move delivered entries to Mongo or leave them pending on any error."""
        nonlocal end_signal_received, pcm_conversation_id
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
                raw_conversation_id = fields.get(b"conversation_id") or fields.get(
                    "conversation_id"
                )
                if not raw_conversation_id:
                    raise AudioPersistenceInvariantError(
                        f"Redis WAL entry {_message_id_text(message_id)} has no owner"
                    )
                conversation_id = (
                    raw_conversation_id.decode()
                    if isinstance(raw_conversation_id, bytes)
                    else str(raw_conversation_id)
                )

                if audio_data:
                    if pcm_conversation_id and pcm_conversation_id != conversation_id:
                        await commit_and_ack_buffer()
                    if pcm_conversation_id is None:
                        pcm_conversation_id = conversation_id
                        seen_conversations.add(conversation_id)
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

    await redis_client.delete(f"audio_persistence:session:{session_id}")
    logger.info(
        f"🎵 Durable audio persistence complete for session {session_id}: "
        f"{total_mongo_chunks_written} new Mongo chunks, "
        f"{total_pcm_bytes} PCM bytes, terminal_marker={end_signal_received}"
    )
    return {
        "session_id": session_id,
        "conversation_count": len(seen_conversations),
        "total_mongo_chunks": total_mongo_chunks_written,
        "total_pcm_bytes": total_pcm_bytes,
        "total_compressed_bytes": total_compressed_bytes,
        "compression_ratio": compression_ratio,
        "duration_seconds": duration,
        "runtime_seconds": runtime_seconds,
    }


# Enqueue wrapper functions
