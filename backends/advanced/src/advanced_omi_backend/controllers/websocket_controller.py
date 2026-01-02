
"""
WebSocket controller for Chronicle backend.

This module handles WebSocket connections for audio streaming.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import time
import uuid
from functools import partial
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect, Query
from friend_lite.decoder import OmiOpusDecoder

from advanced_omi_backend.auth import websocket_auth
from advanced_omi_backend.client_manager import generate_client_id, get_client_manager
from advanced_omi_backend.constants import OMI_CHANNELS, OMI_SAMPLE_RATE, OMI_SAMPLE_WIDTH
from advanced_omi_backend.utils.audio_utils import process_audio_chunk
from advanced_omi_backend.services.audio_stream import AudioStreamProducer
from advanced_omi_backend.services.audio_stream.producer import get_audio_stream_producer

# Thread pool executors for audio decoding
_DEC_IO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=os.cpu_count() or 4,
    thread_name_prefix="opus_io",
)

# Logging setup
logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")

# Track pending WebSocket connections to prevent race conditions
pending_connections: set[str] = set()


async def parse_wyoming_protocol(ws: WebSocket) -> tuple[dict, Optional[bytes]]:
    """Parse Wyoming protocol: JSON header line followed by optional binary payload.

    Returns:
        Tuple of (header_dict, payload_bytes or None)
    """
    # Read data from WebSocket
    logger.debug(f"parse_wyoming_protocol: About to call ws.receive()")
    message = await ws.receive()
    logger.debug(f"parse_wyoming_protocol: Received message with keys: {message.keys() if message else 'None'}")

    # Handle WebSocket close frame
    if "type" in message and message["type"] == "websocket.disconnect":
        # This is a normal WebSocket close event
        code = message.get("code", 1000)
        reason = message.get("reason", "")
        logger.info(f"📴 WebSocket disconnect received in parse_wyoming_protocol. Code: {code}, Reason: {reason}")
        raise WebSocketDisconnect(code=code, reason=reason)

    # Handle text message (JSON header)
    if "text" in message:
        header_text = message["text"]
        # Wyoming protocol uses newline-terminated JSON
        if not header_text.endswith("\n"):
            header_text += "\n"

        # Parse JSON header
        json_line = header_text.strip()
        header = json.loads(json_line)

        # If payload is expected, read binary data
        payload = None
        payload_length = header.get("payload_length")
        if payload_length is not None and payload_length > 0:
            payload_msg = await ws.receive()
            if "bytes" in payload_msg:
                payload = payload_msg["bytes"]
            else:
                logger.warning(f"Expected binary payload but got: {payload_msg.keys()}")

        return header, payload

    # Handle binary message (invalid - Wyoming protocol requires JSONL headers)
    elif "bytes" in message:
        raise ValueError(
            "Raw binary messages not supported - Wyoming protocol requires JSONL headers"
        )

    else:
        raise ValueError(f"Unexpected WebSocket message type: {message.keys()}")


async def create_client_state(client_id: str, user, device_name: Optional[str] = None):
    """Create and register a new client state."""
    # Get client manager
    client_manager = get_client_manager()

    # Directory where WAV chunks are written
    from pathlib import Path
    CHUNK_DIR = Path("./audio_chunks")  # This will be mounted to ./data/audio_chunks by Docker

    # Use ClientManager for atomic client creation and registration
    client_state = client_manager.create_client(
        client_id, CHUNK_DIR, user.user_id, user.email
    )

    # Also track in persistent mapping (for database queries)
    from advanced_omi_backend.client_manager import track_client_user_relationship
    track_client_user_relationship(client_id, user.user_id)

    # Register client in user model (persistent)
    from advanced_omi_backend.users import register_client_to_user
    await register_client_to_user(user, client_id, device_name)

    return client_state


async def cleanup_client_state(client_id: str):
    """Clean up and remove client state, including cancelling speech detection job and marking session complete."""
    # Cancel the speech detection job for this client
    from advanced_omi_backend.controllers.queue_controller import redis_conn
    from rq.job import Job
    import redis.asyncio as redis

    try:
        job_id_key = f"speech_detection_job:{client_id}"
        job_id_bytes = redis_conn.get(job_id_key)

        if job_id_bytes:
            job_id = job_id_bytes.decode()
            logger.info(f"🛑 Cancelling speech detection job {job_id} for client {client_id}")

            try:
                # Fetch and cancel the job
                job = Job.fetch(job_id, connection=redis_conn)
                job.cancel()
                logger.info(f"✅ Successfully cancelled speech detection job {job_id}")
            except Exception as job_error:
                logger.warning(f"⚠️ Failed to cancel job {job_id}: {job_error}")

            # Clean up the tracking key
            redis_conn.delete(job_id_key)
            logger.info(f"🧹 Cleaned up job tracking key for client {client_id}")
        else:
            logger.debug(f"No speech detection job found for client {client_id}")
    except Exception as e:
        logger.warning(f"⚠️ Error during job cancellation for client {client_id}: {e}")

    # Mark all active sessions for this client as complete AND delete Redis streams
    try:
        # Get async Redis client
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        async_redis = redis.from_url(redis_url, decode_responses=False)

        # Find all session keys for this client and mark them complete
        pattern = f"audio:session:*"
        cursor = 0
        sessions_closed = 0

        while True:
            cursor, keys = await async_redis.scan(cursor, match=pattern, count=100)

            for key in keys:
                # Check if this session belongs to this client
                client_id_bytes = await async_redis.hget(key, "client_id")
                if client_id_bytes and client_id_bytes.decode() == client_id:
                    # Mark session as complete (WebSocket disconnected)
                    await async_redis.hset(key, mapping={
                        "status": "complete",
                        "completed_at": str(time.time()),
                        "completion_reason": "websocket_disconnect"
                    })
                    session_id = key.decode().replace("audio:session:", "")
                    logger.info(f"📊 Marked session {session_id[:12]} as complete (WebSocket disconnect)")
                    sessions_closed += 1

            if cursor == 0:
                break

        if sessions_closed > 0:
            logger.info(f"✅ Closed {sessions_closed} active session(s) for client {client_id}")

        # Delete Redis Streams for this client
        stream_pattern = f"audio:stream:{client_id}"
        stream_key = await async_redis.exists(stream_pattern)
        if stream_key:
            await async_redis.delete(stream_pattern)
            logger.info(f"🧹 Deleted Redis stream: {stream_pattern}")
        else:
            logger.debug(f"No Redis stream found for client {client_id}")

        await async_redis.close()

    except Exception as session_error:
        logger.warning(f"⚠️ Error marking sessions complete for client {client_id}: {session_error}")

    # Use ClientManager for atomic client removal with cleanup
    client_manager = get_client_manager()
    removed = await client_manager.remove_client_with_cleanup(client_id)

    if removed:
        logger.info(f"Client {client_id} cleaned up successfully")
    else:
        logger.warning(f"Client {client_id} was not found for cleanup")


# Shared helper functions for WebSocket handlers
async def _setup_websocket_connection(
    ws: WebSocket,
    token: Optional[str],
    device_name: Optional[str],
    pending_client_id: str,
    connection_type: str
) -> tuple[Optional[str], Optional[object], Optional[object]]:
    """
    Setup WebSocket connection: accept, authenticate, create client state.

    Args:
        ws: WebSocket connection
        token: JWT authentication token
        device_name: Optional device name for client ID
        pending_client_id: Temporary tracking ID
        connection_type: "OMI" or "PCM" for logging

    Returns:
        tuple: (client_id, client_state, user) or (None, None, None) on failure
    """
    # Accept WebSocket first (required before any send/close operations)
    await ws.accept()

    # Authenticate user after accepting connection
    user = await websocket_auth(ws, token)
    if not user:
        # Send error message to client before closing
        try:
            error_msg = json.dumps({
                "type": "error",
                "error": "authentication_failed",
                "message": "Authentication failed. Please log in again and ensure your token is valid.",
                "code": 1008
            }) + "\n"
            await ws.send_text(error_msg)
            application_logger.info("Sent authentication error message to client")
        except Exception as send_error:
            application_logger.warning(f"Failed to send error message: {send_error}")

        # Close connection with appropriate code
        await ws.close(code=1008, reason="Authentication failed")
        return None, None, None

    # Generate proper client_id using user and device_name
    client_id = generate_client_id(user, device_name)

    # Remove from pending now that we have real client_id
    pending_connections.discard(pending_client_id)
    application_logger.info(
        f"🔌 {connection_type} WebSocket connection accepted - User: {user.user_id} ({user.email}), Client: {client_id}"
    )

    # Send ready message for PCM clients
    if connection_type == "PCM":
        try:
            ready_msg = json.dumps({"type": "ready", "message": "WebSocket connection established"}) + "\n"
            await ws.send_text(ready_msg)
            application_logger.debug(f"✅ Sent ready message to {client_id}")
        except Exception as e:
            application_logger.error(f"Failed to send ready message to {client_id}: {e}")

    # Create client state
    client_state = await create_client_state(client_id, user, device_name)

    return client_id, client_state, user


async def _initialize_streaming_session(
    client_state,
    audio_stream_producer,
    user_id: str,
    user_email: str,
    client_id: str,
    audio_format: dict
) -> None:
    """
    Initialize streaming session with Redis and enqueue processing jobs.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        user_id: User ID
        user_email: User email
        client_id: Client ID
        audio_format: Audio format dict from audio-start event
    """
    if hasattr(client_state, 'stream_session_id'):
        application_logger.debug(f"Session already initialized for {client_id}")
        return

    # Initialize stream session
    client_state.stream_session_id = str(uuid.uuid4())
    client_state.stream_chunk_count = 0
    client_state.stream_audio_format = audio_format
    application_logger.info(f"🆔 Created stream session: {client_state.stream_session_id}")

    # Determine transcription provider from config.yml
    from advanced_omi_backend.model_registry import get_models_registry

    registry = get_models_registry()
    if not registry:
        raise ValueError("config.yml not found - cannot determine transcription provider")

    stt_model = registry.get_default("stt")
    if not stt_model:
        raise ValueError("No default STT model configured in config.yml (defaults.stt)")

    provider = stt_model.model_provider.lower()
    if provider not in ["deepgram", "parakeet"]:
        raise ValueError(f"Unsupported STT provider: {provider}. Expected: deepgram or parakeet")

    application_logger.info(f"📋 Using STT provider: {provider} (model: {stt_model.name})")
    
    # Initialize session tracking in Redis
    await audio_stream_producer.init_session(
        session_id=client_state.stream_session_id,
        user_id=user_id,
        client_id=client_id,
        mode="streaming",
        provider=provider
    )

    # Enqueue streaming jobs (speech detection + audio persistence)
    from advanced_omi_backend.controllers.queue_controller import start_streaming_jobs

    job_ids = start_streaming_jobs(
        session_id=client_state.stream_session_id,
        user_id=user_id,
        client_id=client_id
    )

    client_state.speech_detection_job_id = job_ids['speech_detection']
    client_state.audio_persistence_job_id = job_ids['audio_persistence']


async def _finalize_streaming_session(
    client_state,
    audio_stream_producer,
    user_id: str,
    user_email: str,
    client_id: str
) -> None:
    """
    Finalize streaming session: flush buffer, signal workers, enqueue finalize job, cleanup.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        user_id: User ID
        user_email: User email
        client_id: Client ID
    """
    if not hasattr(client_state, 'stream_session_id'):
        application_logger.debug(f"No active session to finalize for {client_id}")
        return

    session_id = client_state.stream_session_id

    try:
        # Flush any remaining buffered audio
        audio_format = getattr(client_state, 'stream_audio_format', {})
        await audio_stream_producer.flush_session_buffer(
            session_id=session_id,
            sample_rate=audio_format.get("rate", 16000),
            channels=audio_format.get("channels", 1),
            sample_width=audio_format.get("width", 2)
        )

        # Send end-of-session signal to workers
        await audio_stream_producer.send_session_end_signal(session_id)

        # Mark session as finalizing
        await audio_stream_producer.finalize_session(session_id)

        # NOTE: Finalize job disabled - open_conversation_job now handles everything
        # The open_conversation_job will:
        # 1. Detect the "finalizing" status
        # 2. Enter 5-second grace period
        # 3. Get audio file path
        # 4. Mark session complete
        # 5. Clean up Redis streams
        # 6. Enqueue batch transcription and memory processing
        #
        # If no speech was detected (open_conversation_job never started):
        # - Audio is discarded (intentional - we only create conversations with speech)
        # - Redis streams are cleaned up by TTL
        #
        # TODO: Consider adding cleanup for no-speech scenarios if needed

        application_logger.info(
            f"✅ Session {session_id[:12]} marked as finalizing - open_conversation_job will handle cleanup"
        )

        # Clear session state
        for attr in ['stream_session_id', 'stream_chunk_count', 'stream_audio_format',
                     'speech_detection_job_id', 'audio_persistence_job_id']:
            if hasattr(client_state, attr):
                delattr(client_state, attr)

    except Exception as finalize_error:
        application_logger.error(
            f"❌ Failed to finalize streaming session: {finalize_error}",
            exc_info=True
        )


async def _publish_audio_to_stream(
    client_state,
    audio_stream_producer,
    audio_data: bytes,
    user_id: str,
    client_id: str,
    sample_rate: int,
    channels: int,
    sample_width: int
) -> None:
    """
    Publish audio chunk to Redis Stream with chunk tracking.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        audio_data: Raw PCM audio bytes
        user_id: User ID
        client_id: Client ID
        sample_rate: Sample rate (Hz)
        channels: Number of channels
        sample_width: Bytes per sample
    """
    if not hasattr(client_state, 'stream_session_id'):
        application_logger.warning(f"⚠️ Received audio chunk before session initialized for {client_id}")
        return

    # Increment chunk count and format chunk ID
    client_state.stream_chunk_count += 1
    chunk_id = f"{client_state.stream_chunk_count:05d}"

    # Publish to Redis Stream using producer
    await audio_stream_producer.add_audio_chunk(
        audio_data=audio_data,
        session_id=client_state.stream_session_id,
        chunk_id=chunk_id,
        user_id=user_id,
        client_id=client_id,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width
    )


async def _handle_omi_audio_chunk(
    client_state,
    audio_stream_producer,
    opus_payload: bytes,
    decode_packet_fn,
    user_id: str,
    client_id: str,
    packet_count: int
) -> None:
    """
    Handle OMI audio chunk: decode Opus to PCM, then publish to stream.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        opus_payload: Opus-encoded audio bytes
        decode_packet_fn: Opus decoder function
        user_id: User ID
        client_id: Client ID
        packet_count: Current packet number for logging
    """
    # Decode Opus to PCM
    start_time = time.time()
    loop = asyncio.get_running_loop()
    pcm_data = await loop.run_in_executor(_DEC_IO_EXECUTOR, decode_packet_fn, opus_payload)
    decode_time = time.time() - start_time

    if pcm_data:
        if packet_count <= 5 or packet_count % 1000 == 0:
            application_logger.debug(
                f"🎵 Decoded OMI packet #{packet_count}: {len(opus_payload)} bytes -> "
                f"{len(pcm_data)} PCM bytes (took {decode_time:.3f}s)"
            )

        # Publish decoded PCM to Redis Stream
        await _publish_audio_to_stream(
            client_state,
            audio_stream_producer,
            pcm_data,
            user_id,
            client_id,
            OMI_SAMPLE_RATE,
            OMI_CHANNELS,
            OMI_SAMPLE_WIDTH
        )
    else:
        # Log decode failures for first 5 packets
        if packet_count <= 5:
            application_logger.warning(
                f"❌ Failed to decode OMI packet #{packet_count}: {len(opus_payload)} bytes"
            )


async def _handle_streaming_mode_audio(
    client_state,
    audio_stream_producer,
    audio_data: bytes,
    audio_format: dict,
    user_id: str,
    user_email: str,
    client_id: str
) -> None:
    """
    Handle audio chunk in streaming mode.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        audio_data: Raw PCM audio bytes
        audio_format: Audio format dict (rate, width, channels)
        user_id: User ID
        user_email: User email
        client_id: Client ID
    """
    # Initialize session if needed
    if not hasattr(client_state, 'stream_session_id'):
        await _initialize_streaming_session(
            client_state,
            audio_stream_producer,
            user_id,
            user_email,
            client_id,
            audio_format
        )

    # Publish to Redis Stream
    await _publish_audio_to_stream(
        client_state,
        audio_stream_producer,
        audio_data,
        user_id,
        client_id,
        audio_format.get("rate", 16000),
        audio_format.get("channels", 1),
        audio_format.get("width", 2)
    )


async def _handle_batch_mode_audio(
    client_state,
    audio_data: bytes,
    audio_format: dict,
    client_id: str
) -> None:
    """
    Handle audio chunk in batch mode - accumulate in memory.

    Args:
        client_state: Client state object
        audio_data: Raw PCM audio bytes
        audio_format: Audio format dict
        client_id: Client ID
    """
    # Initialize batch accumulator if needed
    if not hasattr(client_state, 'batch_audio_chunks'):
        client_state.batch_audio_chunks = []
        client_state.batch_audio_format = audio_format
        application_logger.info(f"📦 Started batch audio accumulation for {client_id}")

    # Accumulate audio
    client_state.batch_audio_chunks.append(audio_data)
    application_logger.debug(
        f"📦 Accumulated chunk #{len(client_state.batch_audio_chunks)} ({len(audio_data)} bytes) for {client_id}"
    )


async def _handle_audio_chunk(
    client_state,
    audio_stream_producer,
    audio_data: bytes,
    audio_format: dict,
    user_id: str,
    user_email: str,
    client_id: str
) -> None:
    """
    Route audio chunk to appropriate mode handler (streaming or batch).

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        audio_data: Raw PCM audio bytes
        audio_format: Audio format dict
        user_id: User ID
        user_email: User email
        client_id: Client ID
    """
    recording_mode = getattr(client_state, 'recording_mode', 'batch')

    if recording_mode == "streaming":
        await _handle_streaming_mode_audio(
            client_state, audio_stream_producer, audio_data,
            audio_format, user_id, user_email, client_id
        )
    else:
        await _handle_batch_mode_audio(
            client_state, audio_data, audio_format, client_id
        )


async def _handle_audio_session_start(
    client_state,
    audio_format: dict,
    client_id: str
) -> tuple[bool, str]:
    """
    Handle audio-start event - set mode and switch to audio streaming.

    Args:
        client_state: Client state object
        audio_format: Audio format dict with mode
        client_id: Client ID

    Returns:
        (audio_streaming_flag, recording_mode)
    """
    recording_mode = audio_format.get("mode", "batch")
    client_state.recording_mode = recording_mode

    application_logger.info(
        f"🎙️ Audio session started for {client_id} - "
        f"Format: {audio_format.get('rate')}Hz, "
        f"{audio_format.get('width')}bytes, "
        f"{audio_format.get('channels')}ch, "
        f"Mode: {recording_mode}"
    )

    return True, recording_mode  # Switch to audio streaming mode


async def _handle_audio_session_stop(
    client_state,
    audio_stream_producer,
    user_id: str,
    user_email: str,
    client_id: str
) -> bool:
    """
    Handle audio-stop event - finalize session based on mode.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        user_id: User ID
        user_email: User email
        client_id: Client ID

    Returns:
        False to switch back to control mode
    """
    recording_mode = getattr(client_state, 'recording_mode', 'batch')
    application_logger.info(f"🛑 Audio session stopped for {client_id} (mode: {recording_mode})")

    if recording_mode == "streaming":
        await _finalize_streaming_session(
            client_state, audio_stream_producer,
            user_id, user_email, client_id
        )
    else:
        await _process_batch_audio_complete(
            client_state, user_id, user_email, client_id
        )

    return False  # Switch back to control mode


async def _process_batch_audio_complete(
    client_state,
    user_id: str,
    user_email: str,
    client_id: str
) -> None:
    """
    Process completed batch audio: write file, create conversation, enqueue jobs.

    Args:
        client_state: Client state with batch_audio_chunks
        user_id: User ID
        user_email: User email
        client_id: Client ID
    """
    if not hasattr(client_state, 'batch_audio_chunks') or not client_state.batch_audio_chunks:
        application_logger.warning(f"⚠️ Batch mode: No audio chunks accumulated for {client_id}")
        return

    try:
        from advanced_omi_backend.utils.audio_utils import write_audio_file
        from advanced_omi_backend.models.conversation import create_conversation

        # Combine all chunks
        complete_audio = b''.join(client_state.batch_audio_chunks)
        application_logger.info(
            f"📦 Batch mode: Combined {len(client_state.batch_audio_chunks)} chunks into {len(complete_audio)} bytes"
        )

        # Generate audio UUID and timestamp
        audio_uuid = str(uuid.uuid4())
        timestamp = int(time.time() * 1000)

        # Write audio file and create AudioFile entry
        relative_audio_path, file_path, duration = await write_audio_file(
            raw_audio_data=complete_audio,
            audio_uuid=audio_uuid,
            source="websocket",
            client_id=client_id,
            user_id=user_id,
            user_email=user_email,
            timestamp=timestamp,
            validate=False  # PCM data, not WAV
        )

        application_logger.info(
            f"✅ Batch mode: Wrote audio file {relative_audio_path} ({duration:.1f}s)"
        )

        # Create conversation immediately for batch audio (conversation_id auto-generated)
        version_id = str(uuid.uuid4())

        conversation = create_conversation(
            audio_uuid=audio_uuid,
            user_id=user_id,
            client_id=client_id,
            title="Batch Recording",
            summary="Processing batch audio..."
        )
        conversation.audio_path = relative_audio_path
        await conversation.insert()
        conversation_id = conversation.conversation_id  # Get the auto-generated ID

        application_logger.info(f"📝 Batch mode: Created conversation {conversation_id}")

        # Enqueue post-conversation processing job chain
        from advanced_omi_backend.controllers.queue_controller import start_post_conversation_jobs

        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            audio_uuid=audio_uuid,
            audio_file_path=file_path,
            user_id=None,  # Will be read from conversation in DB by jobs
            post_transcription=True,  # Run batch transcription for uploads
            client_id=client_id  # Pass client_id for UI tracking
        )

        application_logger.info(
            f"✅ Batch mode: Enqueued job chain for {conversation_id} - "
            f"transcription ({job_ids['transcription']}) → "
            f"speaker ({job_ids['speaker_recognition']}) → "
            f"memory ({job_ids['memory']})"
        )

        # Clear accumulated chunks
        client_state.batch_audio_chunks = []

    except Exception as batch_error:
        application_logger.error(
            f"❌ Batch mode processing failed: {batch_error}",
            exc_info=True
        )


async def handle_omi_websocket(
    ws: WebSocket,
    token: Optional[str] = None,
    device_name: Optional[str] = None,
):
    """Handle OMI WebSocket connections with Opus decoding."""
    # Generate pending client_id to track connection even if auth fails
    pending_client_id = f"pending_{uuid.uuid4()}"
    pending_connections.add(pending_client_id)

    client_id = None
    client_state = None

    try:
        # Setup connection (accept, auth, create client state)
        client_id, client_state, user = await _setup_websocket_connection(
            ws, token, device_name, pending_client_id, "OMI"
        )
        if not user:
            return

        # OMI-specific: Setup Opus decoder
        decoder = OmiOpusDecoder()
        _decode_packet = partial(decoder.decode_packet, strip_header=False)

        # Get singleton audio stream producer
        audio_stream_producer = get_audio_stream_producer()

        packet_count = 0
        total_bytes = 0

        while True:
            # Parse Wyoming protocol
            header, payload = await parse_wyoming_protocol(ws)

            if header["type"] == "audio-start":
                # Handle audio session start
                application_logger.info(f"🎙️ OMI audio session started for {client_id}")
                await _initialize_streaming_session(
                    client_state,
                    audio_stream_producer,
                    user.user_id,
                    user.email,
                    client_id,
                    header.get("data", {"rate": OMI_SAMPLE_RATE, "width": OMI_SAMPLE_WIDTH, "channels": OMI_CHANNELS})
                )

            elif header["type"] == "audio-chunk" and payload:
                packet_count += 1
                total_bytes += len(payload)

                # Log progress
                if packet_count <= 5 or packet_count % 1000 == 0:
                    application_logger.info(
                        f"🎵 Received OMI audio chunk #{packet_count}: {len(payload)} bytes"
                    )

                # Handle OMI audio chunk (Opus decode + publish to stream)
                await _handle_omi_audio_chunk(
                    client_state,
                    audio_stream_producer,
                    payload,
                    _decode_packet,
                    user.user_id,
                    client_id,
                    packet_count
                )

                # Log progress every 1000th packet
                if packet_count % 1000 == 0:
                    application_logger.info(
                        f"📊 Processed {packet_count} OMI packets ({total_bytes} bytes total)"
                    )

            elif header["type"] == "audio-stop":
                # Handle audio session stop
                application_logger.info(
                    f"🛑 OMI audio session stopped for {client_id} - "
                    f"Total chunks: {packet_count}, Total bytes: {total_bytes}"
                )

                # Finalize session using helper function
                await _finalize_streaming_session(
                    client_state,
                    audio_stream_producer,
                    user.user_id,
                    user.email,
                    client_id
                )

                # Reset counters for next session
                packet_count = 0
                total_bytes = 0

            else:
                # Unknown event type
                application_logger.debug(
                    f"Ignoring Wyoming event type '{header['type']}' for OMI client {client_id}"
                )

    except WebSocketDisconnect:
        application_logger.info(
            f"🔌 WebSocket disconnected - Client: {client_id}, Packets: {packet_count}, Total bytes: {total_bytes}"
        )
    except Exception as e:
        application_logger.error(f"❌ WebSocket error for client {client_id}: {e}", exc_info=True)
    finally:
        # Clean up pending connection tracking
        pending_connections.discard(pending_client_id)

        # Ensure cleanup happens even if client_id is None
        if client_id:
            try:
                # Clean up client state
                await cleanup_client_state(client_id)
            except Exception as cleanup_error:
                application_logger.error(
                    f"Error during cleanup for client {client_id}: {cleanup_error}", exc_info=True
                )


async def handle_pcm_websocket(
    ws: WebSocket,
    token: Optional[str] = None,
    device_name: Optional[str] = None
):
    """Handle PCM WebSocket connections with batch and streaming mode support."""
    # Generate pending client_id to track connection even if auth fails
    pending_client_id = f"pending_{uuid.uuid4()}"
    pending_connections.add(pending_client_id)

    client_id = None
    client_state = None

    try:
        # Setup connection (accept, auth, create client state)
        client_id, client_state, user = await _setup_websocket_connection(
            ws, token, device_name, pending_client_id, "PCM"
        )
        if not user:
            return

        # Get singleton audio stream producer
        audio_stream_producer = get_audio_stream_producer()

        packet_count = 0
        total_bytes = 0
        audio_streaming = False  # Track if audio session is active

        while True:
            try:
                if not audio_streaming:
                    # Control message mode - parse Wyoming protocol
                    application_logger.debug(f"🔄 Control mode for {client_id}, WebSocket state: {ws.client_state if hasattr(ws, 'client_state') else 'unknown'}")
                    application_logger.debug(f"📨 About to receive control message for {client_id}")
                    header, payload = await parse_wyoming_protocol(ws)
                    application_logger.debug(f"✅ Received message type: {header.get('type')} for {client_id}")

                    if header["type"] == "audio-start":
                        application_logger.debug(f"🎙️ Processing audio-start for {client_id}")
                        # Handle audio session start using helper function
                        audio_streaming, recording_mode = await _handle_audio_session_start(
                            client_state,
                            header.get("data", {}),
                            client_id
                        )
                        continue  # Continue to audio streaming mode
                    
                    elif header["type"] == "ping":
                        # Handle keepalive ping from frontend
                        application_logger.debug(f"🏓 Received ping from {client_id}")
                        continue
                    
                    else:
                        # Unknown control message type
                        application_logger.debug(
                            f"Ignoring Wyoming control event type '{header['type']}' for {client_id}"
                        )
                        continue
                        
                else:
                    # Audio streaming mode - receive raw bytes (like speaker recognition)
                    application_logger.debug(f"🎵 Audio streaming mode for {client_id} - waiting for audio data")
                    
                    try:
                        # Receive raw audio bytes or check for control messages
                        message = await ws.receive()
                        
                        
                        # Check if it's a disconnect
                        if "type" in message and message["type"] == "websocket.disconnect":
                            code = message.get("code", 1000)
                            reason = message.get("reason", "")
                            application_logger.info(f"🔌 WebSocket disconnect during audio streaming for {client_id}. Code: {code}, Reason: {reason}")
                            break
                        
                        # Check if it's a text message (control message like audio-stop)
                        if "text" in message:
                            try:
                                control_header = json.loads(message["text"].strip())
                                if control_header.get("type") == "audio-stop":
                                    # Handle audio session stop using helper function
                                    audio_streaming = await _handle_audio_session_stop(
                                        client_state,
                                        audio_stream_producer,
                                        user.user_id,
                                        user.email,
                                        client_id
                                    )
                                    # Reset counters for next session
                                    packet_count = 0
                                    total_bytes = 0
                                    continue
                                elif control_header.get("type") == "ping":
                                    application_logger.debug(f"🏓 Received ping during streaming from {client_id}")
                                    continue
                                elif control_header.get("type") == "audio-start":
                                    # Handle duplicate audio-start messages gracefully (idempotent behavior)
                                    application_logger.info(f"🔄 Ignoring duplicate audio-start message during streaming for {client_id}")
                                    continue
                                elif control_header.get("type") == "audio-chunk":
                                    # Handle Wyoming protocol audio-chunk with binary payload
                                    payload_length = control_header.get("payload_length")
                                    if payload_length and payload_length > 0:
                                        # Receive the binary audio data
                                        payload_msg = await ws.receive()
                                        if "bytes" in payload_msg:
                                            audio_data = payload_msg["bytes"]
                                            packet_count += 1
                                            total_bytes += len(audio_data)

                                            application_logger.debug(f"🎵 Received audio chunk #{packet_count}: {len(audio_data)} bytes")

                                            # Route to appropriate mode handler
                                            audio_format = control_header.get("data", {})
                                            await _handle_audio_chunk(
                                                client_state,
                                                audio_stream_producer,
                                                audio_data,
                                                audio_format,
                                                user.user_id,
                                                user.email,
                                                client_id
                                            )
                                        else:
                                            application_logger.warning(f"Expected binary payload for audio-chunk, got: {payload_msg.keys()}")
                                    else:
                                        application_logger.warning(f"audio-chunk missing payload_length: {payload_length}")
                                    continue
                                else:
                                    application_logger.warning(f"Unknown control message during streaming: {control_header.get('type')}")
                                    continue
                                            
                            except json.JSONDecodeError:
                                application_logger.warning(f"Invalid control message during streaming for {client_id}")
                                continue
                        
                        # Check if it's binary data (raw audio without Wyoming protocol)
                        elif "bytes" in message:
                            # Raw binary audio data (legacy support)
                            audio_data = message["bytes"]
                            packet_count += 1
                            total_bytes += len(audio_data)

                            application_logger.debug(f"🎵 Received raw audio chunk #{packet_count}: {len(audio_data)} bytes")

                            # Route to appropriate mode handler with default format
                            default_format = {"rate": 16000, "width": 2, "channels": 1}
                            await _handle_audio_chunk(
                                client_state,
                                audio_stream_producer,
                                audio_data,
                                default_format,
                                user.user_id,
                                user.email,
                                client_id
                            )
                        
                        else:
                            application_logger.warning(f"Unexpected message format in streaming mode: {message.keys()}")
                            continue
                            
                    except Exception as streaming_error:
                        application_logger.error(f"Error in audio streaming mode: {streaming_error}")
                        if "disconnect" in str(streaming_error).lower():
                            break
                        continue

            except WebSocketDisconnect as e:
                application_logger.info(
                    f"🔌 WebSocket disconnected during message processing for {client_id}. "
                    f"Code: {e.code}, Reason: {e.reason}"
                )
                break  # Exit the loop on disconnect
            except json.JSONDecodeError as e:
                application_logger.error(
                    f"❌ JSON decode error in Wyoming protocol for {client_id}: {e}"
                )
                continue  # Skip this message but don't disconnect
            except ValueError as e:
                application_logger.error(
                    f"❌ Protocol error for {client_id}: {e}"
                )
                continue  # Skip this message but don't disconnect
            except RuntimeError as e:
                # Handle "Cannot call receive once a disconnect message has been received"
                if "disconnect" in str(e).lower():
                    application_logger.info(
                        f"🔌 WebSocket already disconnected for {client_id}: {e}"
                    )
                    break  # Exit the loop on disconnect
                else:
                    application_logger.error(
                        f"❌ Runtime error for {client_id}: {e}", exc_info=True
                    )
                    continue
            except Exception as e:
                application_logger.error(
                    f"❌ Unexpected error processing message for {client_id}: {e}", exc_info=True
                )
                # Check if it's a connection-related error
                error_msg = str(e).lower()
                if "disconnect" in error_msg or "closed" in error_msg or "receive" in error_msg:
                    application_logger.info(
                        f"🔌 Connection issue detected for {client_id}, exiting loop"
                    )
                    break
                else:
                    continue  # Skip this message for other errors
                
    except WebSocketDisconnect:
        application_logger.info(
            f"🔌 PCM WebSocket disconnected - Client: {client_id}, Packets: {packet_count}, Total bytes: {total_bytes}"
        )
    except Exception as e:
        application_logger.error(
            f"❌ PCM WebSocket error for client {client_id}: {e}", exc_info=True
        )
    finally:
        # Clean up pending connection tracking
        pending_connections.discard(pending_client_id)

        # Ensure cleanup happens even if client_id is None
        if client_id:
            try:
                # Clean up client state
                await cleanup_client_state(client_id)
            except Exception as cleanup_error:
                application_logger.error(
                    f"Error during cleanup for client {client_id}: {cleanup_error}", exc_info=True
                )
