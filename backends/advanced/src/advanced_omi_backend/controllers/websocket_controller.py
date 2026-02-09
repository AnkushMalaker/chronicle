
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

import redis.asyncio as redis
from fastapi import Query, WebSocket, WebSocketDisconnect
from friend_lite.decoder import OmiOpusDecoder
from starlette.websockets import WebSocketState

from advanced_omi_backend.auth import websocket_auth
from advanced_omi_backend.client_manager import generate_client_id, get_client_manager
from advanced_omi_backend.constants import (
    OMI_CHANNELS,
    OMI_SAMPLE_RATE,
    OMI_SAMPLE_WIDTH,
)
from advanced_omi_backend.controllers.session_controller import mark_session_complete
from advanced_omi_backend.services.audio_stream import AudioStreamProducer
from advanced_omi_backend.services.audio_stream.producer import (
    get_audio_stream_producer,
)
from advanced_omi_backend.utils.audio_utils import process_audio_chunk

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


async def subscribe_to_interim_results(websocket: WebSocket, session_id: str) -> None:
    """
    Subscribe to interim transcription results from Redis Pub/Sub and forward to client WebSocket.

    Runs as background task during WebSocket connection. Listens for interim and final
    transcription results published by the Deepgram streaming consumer and forwards them
    to the connected client for real-time transcript display.

    Args:
        websocket: Connected WebSocket client
        session_id: Session ID (client_id) to subscribe to

    Note:
        This task runs continuously until the WebSocket disconnects or the task is cancelled.
        Results are published to Redis Pub/Sub channel: transcription:interim:{session_id}
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    try:
        # Create Redis client for Pub/Sub
        redis_client = await redis.from_url(redis_url, decode_responses=True)

        # Create Pub/Sub instance
        pubsub = redis_client.pubsub()

        # Subscribe to interim results channel for this session
        channel = f"transcription:interim:{session_id}"
        await pubsub.subscribe(channel)

        logger.info(f"📢 Subscribed to interim results channel: {channel}")

        # Listen for messages
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)

                if message and message['type'] == 'message':
                    # Parse result data
                    try:
                        result_data = json.loads(message['data'])

                        # Forward to client WebSocket
                        await websocket.send_json({
                            "type": "interim_transcript",
                            "data": result_data
                        })

                        # Log for debugging
                        is_final = result_data.get("is_final", False)
                        text_preview = result_data.get("text", "")[:50]
                        result_type = "FINAL" if is_final else "interim"
                        logger.debug(f"✉️ Forwarded {result_type} result to client {session_id}: {text_preview}...")

                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse interim result JSON: {e}")
                    except Exception as send_error:
                        logger.error(f"Failed to send interim result to client {session_id}: {send_error}")
                        # WebSocket might be closed, exit loop
                        break

            except asyncio.TimeoutError:
                # No message received, continue waiting
                continue
            except asyncio.CancelledError:
                logger.info(f"Interim results subscriber cancelled for session {session_id}")
                break
            except Exception as e:
                logger.error(f"Error in interim results subscriber for {session_id}: {e}", exc_info=True)
                break

    except Exception as e:
        logger.error(f"Failed to initialize interim results subscriber for {session_id}: {e}", exc_info=True)
    finally:
        try:
            # Unsubscribe and close connections
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await redis_client.aclose()
            logger.info(f"🔕 Unsubscribed from interim results channel: {channel}")
        except Exception as cleanup_error:
            logger.error(f"Error cleaning up interim results subscriber: {cleanup_error}")


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

    # Also track in persistent mapping (for database queries + cross-container Redis)
    from advanced_omi_backend.client_manager import track_client_user_relationship_async
    await track_client_user_relationship_async(client_id, user.user_id)

    # Register client in user model (persistent)
    from advanced_omi_backend.users import register_client_to_user
    await register_client_to_user(user, client_id, device_name)

    return client_state


async def cleanup_client_state(client_id: str):
    """
    Clean up and remove client state, marking session complete.

    Note: We do NOT cancel the speech detection job here because:
    1. The job needs to process all audio data that was already sent
    2. If speech was detected, it should create a conversation
    3. The job will complete naturally when it sees session status = "finalizing"
    4. The job has a grace period (15s) to wait for final transcription
    5. RQ's job_timeout (24h) prevents jobs from hanging forever
    """
    # Note: Previously we cancelled the speech detection job here, but this prevented
    # conversations from being created when WebSocket disconnects mid-recording.
    # The speech detection job now monitors session status and completes naturally.
    import redis.asyncio as redis

    logger.info(f"🔄 Letting speech detection job complete naturally for client {client_id} (if running)")

    # Mark all active sessions for this client as complete AND delete Redis streams
    try:
        # Get async Redis client
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        async_redis = redis.from_url(redis_url, decode_responses=False)

        # Get audio stream producer for finalization
        from advanced_omi_backend.services.audio_stream.producer import (
            get_audio_stream_producer,
        )
        audio_stream_producer = get_audio_stream_producer()

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
                    session_id = key.decode().replace("audio:session:", "")

                    # Check session status
                    status_bytes = await async_redis.hget(key, "status")
                    status = status_bytes.decode() if status_bytes else None

                    # If session is still active, finalize it first (sets status + completion_reason atomically)
                    if status in ["active", None]:
                        logger.info(f"📊 Finalizing active session {session_id[:12]} due to WebSocket disconnect")
                        await audio_stream_producer.finalize_session(session_id, completion_reason="websocket_disconnect")

                    # Mark session as complete (WebSocket disconnected)
                    await mark_session_complete(async_redis, session_id, "websocket_disconnect")
                    sessions_closed += 1

            if cursor == 0:
                break

        if sessions_closed > 0:
            logger.info(f"✅ Closed {sessions_closed} active session(s) for client {client_id}")

        # Set TTL on Redis Streams for this client (allows consumer groups to finish processing)
        stream_pattern = f"audio:stream:{client_id}"
        stream_key = await async_redis.exists(stream_pattern)
        if stream_key:
            # Check how many messages are in the stream
            stream_length = await async_redis.xlen(stream_pattern)

            # Check for pending messages in consumer groups
            pending_count = 0
            try:
                # Check streaming-transcription consumer group for pending messages
                pending_info = await async_redis.xpending(stream_pattern, "streaming-transcription")
                if pending_info:
                    pending_count = pending_info.get('pending', 0)
            except Exception as e:
                # Consumer group might not exist yet - that's ok
                logger.debug(f"No consumer group for {stream_pattern}: {e}")

            if stream_length > 0 or pending_count > 0:
                logger.warning(
                    f"⚠️ Closing {stream_pattern} with unprocessed data: "
                    f"{stream_length} messages in stream, {pending_count} pending in consumer group"
                )

            await async_redis.expire(stream_pattern, 60)  # 60 second TTL for consumer group fan-out
            logger.info(f"⏰ Set 60s TTL on Redis stream: {stream_pattern}")
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
    audio_format: dict,
    websocket: Optional[WebSocket] = None
) -> Optional[asyncio.Task]:
    """
    Initialize streaming session with Redis and enqueue processing jobs.

    Args:
        client_state: Client state object
        audio_stream_producer: Audio stream producer instance
        user_id: User ID
        user_email: User email
        client_id: Client ID
        audio_format: Audio format dict from audio-start event
        websocket: Optional WebSocket connection to launch interim results subscriber

    Returns:
        Interim results subscriber task if websocket provided and session initialized, None otherwise
    """
    application_logger.info(
        f"🔴 BACKEND: _initialize_streaming_session called for {client_id}"
    )

    if hasattr(client_state, 'stream_session_id'):
        application_logger.debug(f"Session already initialized for {client_id}")
        return None

    # Initialize stream session - use client_id as session_id for predictable lookup
    # All other session metadata goes to Redis (single source of truth)
    client_state.stream_session_id = client_state.client_id
    application_logger.info(f"🆔 Created stream session: {client_state.stream_session_id}")

    # Determine transcription provider from config.yml
    from advanced_omi_backend.model_registry import get_models_registry

    registry = get_models_registry()
    if not registry:
        raise ValueError("config.yml not found - cannot determine transcription provider")

    stt_model = registry.get_default("stt")
    if not stt_model:
        raise ValueError("No default STT model configured in config.yml (defaults.stt)")

    # Use model_provider for session tracking (generic, not validated against hardcoded list)
    provider = stt_model.model_provider.lower() if stt_model.model_provider else stt_model.name

    application_logger.info(f"📋 Using STT provider: {provider} (model: {stt_model.name})")

    # Initialize session tracking in Redis (SINGLE SOURCE OF TRUTH for session metadata)
    # This includes user_email, connection info, audio format, chunk counters, job IDs, etc.
    connection_id = f"ws_{client_id}_{int(time.time())}"
    await audio_stream_producer.init_session(
        session_id=client_state.stream_session_id,
        user_id=user_id,
        client_id=client_id,
        user_email=user_email,
        connection_id=connection_id,
        mode="streaming",
        provider=provider
    )

    # Store audio format in Redis session (not in ClientState)
    import json

    from advanced_omi_backend.services.audio_stream.producer import (
        get_audio_stream_producer,
    )
    session_key = f"audio:session:{client_state.stream_session_id}"
    redis_client = audio_stream_producer.redis_client
    await redis_client.hset(session_key, "audio_format", json.dumps(audio_format))

    # Enqueue streaming jobs (speech detection + audio persistence)
    from advanced_omi_backend.controllers.queue_controller import start_streaming_jobs

    job_ids = start_streaming_jobs(
        session_id=client_state.stream_session_id,
        user_id=user_id,
        client_id=client_id
    )

    # Store job IDs in Redis session (not in ClientState)
    await audio_stream_producer.update_session_job_ids(
        session_id=client_state.stream_session_id,
        speech_detection_job_id=job_ids['speech_detection'],
        audio_persistence_job_id=job_ids['audio_persistence']
    )

    # Note: Placeholder conversation creation is handled by the audio persistence job,
    # which reads the always_persist_enabled setting from global config.

    # Launch interim results subscriber if WebSocket provided
    subscriber_task = None
    if websocket:
        subscriber_task = asyncio.create_task(
            subscribe_to_interim_results(websocket, client_state.stream_session_id)
        )
        application_logger.info(f"📡 Launched interim results subscriber for session {client_state.stream_session_id}")

    return subscriber_task


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

        # Mark session as finalizing with user_stopped reason (audio-stop event)
        await audio_stream_producer.finalize_session(session_id, completion_reason="user_stopped")

        # Store markers in Redis so open_conversation_job can persist them
        if client_state.markers:
            session_key = f"audio:session:{session_id}"
            await audio_stream_producer.redis_client.hset(
                session_key, "markers", json.dumps(client_state.markers)
            )
            client_state.markers.clear()

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

        # Clear session state from ClientState (only stream_session_id is stored there now)
        # All other session metadata lives in Redis (single source of truth)
        if hasattr(client_state, 'stream_session_id'):
            delattr(client_state, 'stream_session_id')

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

    session_id = client_state.stream_session_id

    # Increment chunk count in Redis (single source of truth) and format chunk ID
    session_key = f"audio:session:{session_id}"
    redis_client = audio_stream_producer.redis_client
    chunk_count = await redis_client.hincrby(session_key, "chunks_published", 1)
    chunk_id = f"{chunk_count:05d}"

    # Publish to Redis Stream using producer
    await audio_stream_producer.add_audio_chunk(
        audio_data=audio_data,
        session_id=session_id,
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
    client_id: str,
    websocket: Optional[WebSocket] = None
) -> Optional[asyncio.Task]:
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
        websocket: Optional WebSocket connection to launch interim results subscriber

    Returns:
        Interim results subscriber task if websocket provided and session initialized, None otherwise
    """
    # Initialize session if needed
    subscriber_task = None
    if not hasattr(client_state, 'stream_session_id'):
        subscriber_task = await _initialize_streaming_session(
            client_state,
            audio_stream_producer,
            user_id,
            user_email,
            client_id,
            audio_format,
            websocket=websocket  # Pass WebSocket to launch interim results subscriber
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

    return subscriber_task


async def _handle_batch_mode_audio(
    client_state,
    audio_data: bytes,
    audio_format: dict,
    client_id: str
) -> None:
    """
    Handle audio chunk in batch mode with rolling 30-minute limit.

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
        client_state.batch_audio_bytes = 0  # Track total bytes
        client_state.batch_chunks_processed = 0  # Track how many batches processed
        application_logger.info(f"📦 Started batch audio accumulation for {client_id}")

    # Accumulate audio
    client_state.batch_audio_chunks.append(audio_data)
    client_state.batch_audio_bytes += len(audio_data)
    application_logger.debug(
        f"📦 Accumulated chunk #{len(client_state.batch_audio_chunks)} ({len(audio_data)} bytes) for {client_id}"
    )

    # Calculate duration: sample_rate * width * channels = bytes/second
    sample_rate = audio_format.get("rate", 16000)
    width = audio_format.get("width", 2)
    channels = audio_format.get("channels", 1)
    bytes_per_second = sample_rate * width * channels

    accumulated_seconds = client_state.batch_audio_bytes / bytes_per_second
    MAX_BATCH_SECONDS = 30 * 60  # 30 minutes

    # Check if we've hit the 30-minute limit
    if accumulated_seconds >= MAX_BATCH_SECONDS:
        application_logger.warning(
            f"⚠️ Batch accumulation reached 30-minute limit "
            f"({accumulated_seconds:.1f}s, {client_state.batch_audio_bytes / 1024 / 1024:.1f} MB). "
            f"Processing batch #{client_state.batch_chunks_processed + 1}..."
        )

        # Process this batch (will create conversation and transcribe)
        await _process_rolling_batch(
            client_state,
            user_id=client_state.user_id,  # Need to store these on session start
            user_email=client_state.user_email,
            client_id=client_state.client_id,
            batch_number=client_state.batch_chunks_processed + 1
        )

        # Clear buffer for next batch
        client_state.batch_audio_chunks = []
        client_state.batch_audio_bytes = 0
        client_state.batch_chunks_processed += 1

        application_logger.info(
            f"✅ Rolled batch #{client_state.batch_chunks_processed}. "
            f"Starting fresh accumulation for next 30 minutes."
        )


async def _handle_audio_chunk(
    client_state,
    audio_stream_producer,
    audio_data: bytes,
    audio_format: dict,
    user_id: str,
    user_email: str,
    client_id: str,
    websocket: Optional[WebSocket] = None
) -> Optional[asyncio.Task]:
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
        websocket: Optional WebSocket connection to launch interim results subscriber

    Returns:
        Interim results subscriber task if websocket provided and streaming mode, None otherwise
    """
    recording_mode = getattr(client_state, 'recording_mode', 'batch')

    if recording_mode == "streaming":
        return await _handle_streaming_mode_audio(
            client_state, audio_stream_producer, audio_data,
            audio_format, user_id, user_email, client_id,
            websocket=websocket
        )
    else:
        await _handle_batch_mode_audio(
            client_state, audio_data, audio_format, client_id
        )
        return None


async def _handle_audio_session_start(
    client_state,
    audio_format: dict,
    client_id: str,
    websocket: Optional[WebSocket] = None
) -> tuple[bool, str]:
    """
    Handle audio-start event - validate mode and set recording mode.

    Args:
        client_state: Client state object
        audio_format: Audio format dict with mode
        client_id: Client ID
        websocket: Optional WebSocket connection (for WebUI error messages)

    Returns:
        (audio_streaming_flag, recording_mode)
    """
    from advanced_omi_backend.services.transcription import is_transcription_available

    recording_mode = audio_format.get("mode", "batch")

    application_logger.info(
        f"🔴 BACKEND: Received audio-start for {client_id} - "
        f"mode={recording_mode}, full format={audio_format}"
    )

    # Store on client state for later use
    client_state.recording_mode = recording_mode

    # VALIDATION: Check if streaming mode is available
    if recording_mode == "streaming":
        if not is_transcription_available("streaming"):
            error_msg = (
                "Streaming transcription not available. "
                "Please use Batch mode or configure a streaming STT provider (defaults.stt_stream in config.yml)."
            )

            application_logger.warning(
                f"⚠️ Streaming mode requested but stt_stream not configured for {client_id}"
            )

            # Send error to WebSocket client (for WebUI display)
            if websocket and websocket.client_state == WebSocketState.CONNECTED:
                try:
                    error_response = {
                        "type": "error",
                        "error": "streaming_not_configured",
                        "message": error_msg,
                        "code": 400
                    }
                    await websocket.send_json(error_response)
                    application_logger.info(f"📤 Sent streaming error to WebUI client {client_id}")

                    # Close the websocket connection after sending error
                    await websocket.close(code=1008, reason="Streaming transcription not configured")
                    application_logger.info(f"🔌 Closed WebSocket connection for {client_id} due to streaming config error")

                    # Raise ValueError to exit the handler completely
                    raise ValueError(error_msg)
                except ValueError:
                    # Re-raise ValueError to exit handler
                    raise
                except Exception as e:
                    application_logger.error(f"Failed to send error to client: {e}")
                    # Still raise ValueError to exit handler
                    raise ValueError(error_msg)

            # For OMI devices (no websocket), fall back to batch mode silently
            if not websocket:
                application_logger.warning(
                    f"🔄 OMI device {client_id} requested streaming but falling back to batch mode"
                )
                recording_mode = "batch"
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


async def _handle_button_event(
    client_state,
    button_state: str,
    user_id: str,
    client_id: str,
) -> None:
    """Handle a button event from the device.

    Stores a marker on the client state and dispatches granular events
    to the plugin system using typed enums.

    Args:
        client_state: Client state object
        button_state: Button state string (e.g., "SINGLE_TAP", "DOUBLE_TAP")
        user_id: User ID
        client_id: Client ID
    """
    from advanced_omi_backend.plugins.events import (
        BUTTON_STATE_TO_EVENT,
        ButtonState,
    )
    from advanced_omi_backend.services.plugin_service import get_plugin_router

    timestamp = time.time()
    audio_uuid = client_state.current_audio_uuid

    application_logger.info(
        f"🔘 Button event from {client_id}: {button_state} "
        f"(audio_uuid={audio_uuid})"
    )

    # Store marker on client state for later persistence to conversation
    marker = {
        "type": "button_event",
        "state": button_state,
        "timestamp": timestamp,
        "audio_uuid": audio_uuid,
        "client_id": client_id,
    }
    client_state.add_marker(marker)

    # Map device button state to typed plugin event
    try:
        button_state_enum = ButtonState(button_state)
    except ValueError:
        application_logger.warning(f"Unknown button state: {button_state}")
        return

    event = BUTTON_STATE_TO_EVENT.get(button_state_enum)
    if not event:
        application_logger.debug(f"No plugin event mapped for {button_state_enum}")
        return

    # Dispatch granular event to plugin system
    router = get_plugin_router()
    if router:
        await router.dispatch_event(
            event=event.value,
            user_id=user_id,
            data={
                "state": button_state_enum.value,
                "timestamp": timestamp,
                "audio_uuid": audio_uuid,
                "session_id": getattr(client_state, 'stream_session_id', None),
                "client_id": client_id,
            },
        )


async def _process_rolling_batch(
    client_state,
    user_id: str,
    user_email: str,
    client_id: str,
    batch_number: int
) -> None:
    """
    Process accumulated batch audio as a rolling segment.

    Creates conversation titled "Recording Part {batch_number}" and enqueues transcription.

    Args:
        client_state: Client state with batch_audio_chunks
        user_id: User ID
        user_email: User email
        client_id: Client ID
        batch_number: Sequential batch number (1, 2, 3...)
    """
    if not hasattr(client_state, 'batch_audio_chunks') or not client_state.batch_audio_chunks:
        application_logger.warning(f"⚠️ No audio chunks to process for rolling batch")
        return

    try:
        from advanced_omi_backend.models.conversation import create_conversation
        from advanced_omi_backend.utils.audio_chunk_utils import convert_audio_to_chunks

        # Combine chunks
        complete_audio = b''.join(client_state.batch_audio_chunks)
        application_logger.info(
            f"📦 Rolling batch #{batch_number}: Combined {len(client_state.batch_audio_chunks)} chunks "
            f"into {len(complete_audio)} bytes"
        )

        # Get audio format
        audio_format = getattr(client_state, 'batch_audio_format', {})
        sample_rate = audio_format.get("rate", 16000)
        width = audio_format.get("width", 2)
        channels = audio_format.get("channels", 1)

        # Create conversation with batch number in title
        conversation = create_conversation(
            user_id=user_id,
            client_id=client_id,
            title=f"Recording Part {batch_number}",
            summary="Rolling batch processing..."
        )
        await conversation.insert()
        conversation_id = conversation.conversation_id  # Get the auto-generated ID

        # Convert to MongoDB chunks
        num_chunks = await convert_audio_to_chunks(
            conversation_id=conversation_id,
            audio_data=complete_audio,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=width
        )

        # Enqueue transcription job
        from advanced_omi_backend.controllers.queue_controller import (
            JOB_RESULT_TTL,
            transcription_queue,
        )
        from advanced_omi_backend.workers.transcription_jobs import (
            transcribe_full_audio_job,
        )

        version_id = str(uuid.uuid4())
        transcribe_job_id = f"transcribe_rolling_{conversation_id[:12]}_{batch_number}"

        transcription_job = transcription_queue.enqueue(
            transcribe_full_audio_job,
            conversation_id,
            version_id,
            f"rolling_batch_{batch_number}",  # trigger
            job_timeout=900,  # 15 minutes
            result_ttl=JOB_RESULT_TTL,
            job_id=transcribe_job_id,
            description=f"Transcribe rolling batch #{batch_number} {conversation_id[:8]}",
            meta={'conversation_id': conversation_id, 'client_id': client_id, 'batch_number': batch_number}
        )

        application_logger.info(
            f"✅ Rolling batch #{batch_number} created conversation {conversation_id}, "
            f"enqueued transcription job {transcription_job.id}"
        )

    except Exception as e:
        application_logger.error(
            f"❌ Failed to process rolling batch #{batch_number}: {e}",
            exc_info=True
        )


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
        from advanced_omi_backend.models.conversation import create_conversation
        from advanced_omi_backend.utils.audio_chunk_utils import convert_audio_to_chunks

        # Combine all chunks
        complete_audio = b''.join(client_state.batch_audio_chunks)
        application_logger.info(
            f"📦 Batch mode: Combined {len(client_state.batch_audio_chunks)} chunks into {len(complete_audio)} bytes"
        )

        # Timestamp for logging
        timestamp = int(time.time() * 1000)

        # Get audio format from batch metadata (set during audio-start)
        audio_format = getattr(client_state, 'batch_audio_format', {})
        sample_rate = audio_format.get('rate', OMI_SAMPLE_RATE)
        sample_width = audio_format.get('width', OMI_SAMPLE_WIDTH)
        channels = audio_format.get('channels', OMI_CHANNELS)

        # Calculate audio duration
        duration = len(complete_audio) / (sample_rate * sample_width * channels)

        application_logger.info(
            f"✅ Batch mode: Processing audio ({duration:.1f}s)"
        )

        # Create conversation immediately for batch audio (conversation_id auto-generated)
        version_id = str(uuid.uuid4())

        conversation = create_conversation(
            user_id=user_id,
            client_id=client_id,
            title="Batch Recording",
            summary="Processing batch audio..."
        )
        # Attach any markers (e.g., button events) captured during the session
        if client_state.markers:
            conversation.markers = list(client_state.markers)
            client_state.markers.clear()
        await conversation.insert()
        conversation_id = conversation.conversation_id  # Get the auto-generated ID

        application_logger.info(f"📝 Batch mode: Created conversation {conversation_id}")

        # Convert audio directly to MongoDB chunks (no disk intermediary)
        try:
            num_chunks = await convert_audio_to_chunks(
                conversation_id=conversation_id,
                audio_data=complete_audio,
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
            )
            application_logger.info(
                f"📦 Batch mode: Converted to {num_chunks} MongoDB chunks "
                f"(conversation {conversation_id[:12]})"
            )
        except Exception as chunk_error:
            application_logger.error(
                f"Failed to convert batch audio to chunks: {chunk_error}",
                exc_info=True
            )
            # Continue anyway - transcription job will handle it

        # Enqueue batch transcription job first (file uploads need transcription)
        from advanced_omi_backend.controllers.queue_controller import (
            JOB_RESULT_TTL,
            start_post_conversation_jobs,
            transcription_queue,
        )
        from advanced_omi_backend.workers.transcription_jobs import (
            transcribe_full_audio_job,
        )

        version_id = str(uuid.uuid4())
        transcribe_job_id = f"transcribe_{conversation_id[:12]}"

        transcription_job = transcription_queue.enqueue(
            transcribe_full_audio_job,
            conversation_id,
            version_id,
            "batch",  # trigger
            job_timeout=900,  # 15 minutes
            result_ttl=JOB_RESULT_TTL,
            job_id=transcribe_job_id,
            description=f"Transcribe batch audio {conversation_id[:8]}",
            meta={'conversation_id': conversation_id, 'client_id': client_id}
        )

        application_logger.info(f"📥 Batch mode: Enqueued transcription job {transcription_job.id}")

        # Enqueue post-conversation processing job chain (depends on transcription)
        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            user_id=None,  # Will be read from conversation in DB by jobs
            depends_on_job=transcription_job,  # Wait for transcription to complete
            client_id=client_id  # Pass client_id for UI tracking
        )

        application_logger.info(
            f"✅ Batch mode: Enqueued job chain for {conversation_id} - "
            f"transcription ({transcription_job.id}) → "
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
    interim_subscriber_task = None

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
                application_logger.info(f"🔴 BACKEND: Received audio-start in OMI MODE for {client_id} (header={header})")
                application_logger.info(f"🎙️ OMI audio session started for {client_id}")

                # Store user context on client state
                client_state.user_id = user.user_id
                client_state.user_email = user.email
                client_state.client_id = client_id

                interim_subscriber_task = await _initialize_streaming_session(
                    client_state,
                    audio_stream_producer,
                    user.user_id,
                    user.email,
                    client_id,
                    header.get("data", {"rate": OMI_SAMPLE_RATE, "width": OMI_SAMPLE_WIDTH, "channels": OMI_CHANNELS}),
                    websocket=ws  # Pass WebSocket to launch interim results subscriber
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
        # Cancel interim results subscriber task if running
        if interim_subscriber_task and not interim_subscriber_task.done():
            interim_subscriber_task.cancel()
            try:
                await interim_subscriber_task
            except asyncio.CancelledError:
                application_logger.info(f"Interim subscriber task cancelled for {client_id}")
            except Exception as task_error:
                application_logger.error(f"Error cancelling interim subscriber task: {task_error}")

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
    interim_subscriber_task = None

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
                        application_logger.info(f"🔴 BACKEND: Received audio-start in CONTROL MODE for {client_id}")
                        application_logger.debug(f"🎙️ Processing audio-start for {client_id}")

                        # Store user context on client state for rolling batch processing
                        client_state.user_id = user.user_id
                        client_state.user_email = user.email
                        client_state.client_id = client_id

                        # Handle audio session start using helper function (pass websocket for error handling)
                        audio_streaming, recording_mode = await _handle_audio_session_start(
                            client_state,
                            header.get("data", {}),
                            client_id,
                            websocket=ws  # Pass websocket for WebUI error display
                        )

                        # Initialize streaming session
                        if recording_mode == "streaming":
                            application_logger.info(f"🔴 BACKEND: Initializing streaming session for {client_id}")
                            interim_subscriber_task = await _initialize_streaming_session(
                                client_state,
                                audio_stream_producer,
                                user.user_id,
                                user.email,
                                client_id,
                                header.get("data", {}),
                                websocket=ws
                            )

                        continue  # Continue to audio streaming mode
                    
                    elif header["type"] == "ping":
                        # Handle keepalive ping from frontend
                        application_logger.debug(f"🏓 Received ping from {client_id}")
                        continue

                    elif header["type"] == "button-event":
                        button_data = header.get("data", {})
                        button_state = button_data.get("state", "unknown")
                        await _handle_button_event(
                            client_state, button_state, user.user_id, client_id
                        )
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
                                            task = await _handle_audio_chunk(
                                                client_state,
                                                audio_stream_producer,
                                                audio_data,
                                                audio_format,
                                                user.user_id,
                                                user.email,
                                                client_id,
                                                websocket=ws
                                            )
                                            # Store subscriber task if it was created (first streaming chunk)
                                            if task and not interim_subscriber_task:
                                                interim_subscriber_task = task
                                        else:
                                            application_logger.warning(f"Expected binary payload for audio-chunk, got: {payload_msg.keys()}")
                                    else:
                                        application_logger.warning(f"audio-chunk missing payload_length: {payload_length}")
                                    continue
                                elif control_header.get("type") == "button-event":
                                    button_data = control_header.get("data", {})
                                    button_state = button_data.get("state", "unknown")
                                    await _handle_button_event(
                                        client_state, button_state, user.user_id, client_id
                                    )
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
                            task = await _handle_audio_chunk(
                                client_state,
                                audio_stream_producer,
                                audio_data,
                                default_format,
                                user.user_id,
                                user.email,
                                client_id,
                                websocket=ws
                            )
                            # Store subscriber task if it was created (first streaming chunk)
                            if task and not interim_subscriber_task:
                                interim_subscriber_task = task
                        
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
        # Cancel interim results subscriber task if running
        if interim_subscriber_task and not interim_subscriber_task.done():
            interim_subscriber_task.cancel()
            try:
                await interim_subscriber_task
            except asyncio.CancelledError:
                application_logger.info(f"Interim subscriber task cancelled for {client_id}")
            except Exception as task_error:
                application_logger.error(f"Error cancelling interim subscriber task: {task_error}")

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
