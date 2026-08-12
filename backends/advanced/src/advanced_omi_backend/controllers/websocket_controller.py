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
import traceback
import uuid
from functools import partial
from typing import Optional

from chronicle_wearable_sdk.decoder import OmiOpusDecoder
from fastapi import Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from advanced_omi_backend.auth import websocket_auth
from advanced_omi_backend.client_manager import (
    generate_client_id,
    get_client_manager,
    track_client_user_relationship_async,
)
from advanced_omi_backend.config import WS_IDLE_TIMEOUT_SECS
from advanced_omi_backend.constants import (
    OMI_CHANNELS,
    OMI_SAMPLE_RATE,
    OMI_SAMPLE_WIDTH,
    TITLE_NOT_GENERATED,
)
from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    ensure_audio_persistence,
    start_post_conversation_jobs,
    start_streaming_jobs,
    transcription_queue,
)
from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.models.conversation import create_conversation
from advanced_omi_backend.plugins.events import BUTTON_STATE_TO_EVENT, ButtonState
from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.redis_keys import ClientId, device_downlink_channel
from advanced_omi_backend.services.audio_stream.conversation_lifecycle import (
    ensure_active_session_placeholder,
)
from advanced_omi_backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    inspect_stream_retention,
)
from advanced_omi_backend.services.audio_stream.producer import (
    get_audio_stream_producer,
)
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
)
from advanced_omi_backend.services.device_audio import (
    is_opus_streaming_client,
    stop_play_audio,
    stream_play_audio_as_opus,
)
from advanced_omi_backend.services.observability import record_event_sync
from advanced_omi_backend.services.plugin_service import get_plugin_router
from advanced_omi_backend.services.sse_publisher import publish_sse_event_async
from advanced_omi_backend.services.transcription import is_transcription_available
from advanced_omi_backend.services.wakeword.followup import handle_dial_followup
from advanced_omi_backend.users import register_client_to_user, touch_client_last_seen
from advanced_omi_backend.utils.audio_chunk_utils import convert_audio_to_chunks
from advanced_omi_backend.workers.transcription_jobs import transcribe_full_audio_job

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

# Per-client_id locks serializing connection setup. A reconnecting device (same
# client_id) must evict its stale connection and create a fresh ClientState as one
# atomic step, so two concurrent connections can't interleave and orphan state.
_client_setup_locks: dict[str, asyncio.Lock] = {}


def _get_client_setup_lock(client_id: str) -> asyncio.Lock:
    lock = _client_setup_locks.get(client_id)
    if lock is None:
        lock = asyncio.Lock()
        _client_setup_locks[client_id] = lock
    return lock


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
    try:
        # Create Redis client for Pub/Sub
        redis_client = create_async_redis(decode_responses=True)

        # Create Pub/Sub instance
        pubsub = redis_client.pubsub()

        # Subscribe to interim results channel for this session
        channel = f"transcription:interim:{session_id}"
        await pubsub.subscribe(channel)

        logger.info(f"📢 Subscribed to interim results channel: {channel}")

        # Listen for messages
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )

                if message and message["type"] == "message":
                    # Parse result data
                    try:
                        result_data = json.loads(message["data"])

                        # Stop if the socket closed: a result can race the close, and
                        # send_json after close raises the ASGI "websocket.send after
                        # websocket.close" error. Guard instead of catching post-hoc.
                        if websocket.client_state != WebSocketState.CONNECTED:
                            break

                        # Forward to client WebSocket
                        await websocket.send_json(
                            {"type": "interim_transcript", "data": result_data}
                        )

                        # Log for debugging
                        is_final = result_data.get("is_final", False)
                        text_preview = result_data.get("text", "")[:50]
                        result_type = "FINAL" if is_final else "interim"
                        logger.debug(
                            f"✉️ Forwarded {result_type} result to client {session_id}: {text_preview}..."
                        )

                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse interim result JSON: {e}")
                    except Exception as send_error:
                        logger.error(
                            f"Failed to send interim result to client {session_id}: {send_error}"
                        )
                        # WebSocket might be closed, exit loop
                        break

            except asyncio.TimeoutError:
                # No message received, continue waiting
                continue
            except asyncio.CancelledError:
                logger.info(
                    f"Interim results subscriber cancelled for session {session_id}"
                )
                break
            except Exception as e:
                logger.error(
                    f"Error in interim results subscriber for {session_id}: {e}",
                    exc_info=True,
                )
                break

    except Exception as e:
        logger.error(
            f"Failed to initialize interim results subscriber for {session_id}: {e}",
            exc_info=True,
        )
    finally:
        try:
            # Unsubscribe and close connections
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await redis_client.aclose()
            logger.info(f"🔕 Unsubscribed from interim results channel: {channel}")
        except Exception as cleanup_error:
            logger.error(
                f"Error cleaning up interim results subscriber: {cleanup_error}"
            )


async def subscribe_to_device_downlink(
    websocket: WebSocket, client_id: ClientId
) -> None:
    """Forward backend→device control messages from Redis Pub/Sub to the device WebSocket.

    Any backend component (wake-word service, plugins) can push a message to a
    specific device by publishing to ``device:downlink:{client_id}``. Each message
    is a Wyoming-style control frame (e.g. ``{"type": "play-audio", "data": {...}}``)
    which the HAVPE relay's ``handle_backend_messages`` dispatches to the device.

    Runs as a background task for the lifetime of the WebSocket connection.
    """
    if not isinstance(client_id, ClientId):
        raise TypeError("subscribe_to_device_downlink requires ClientId")
    client_id_value = str(client_id)
    channel = str(device_downlink_channel(client_id))
    redis_client = None
    pubsub = None
    opus_stream = is_opus_streaming_client(client_id_value)

    try:
        redis_client = create_async_redis(decode_responses=True)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        logger.info(
            f"🔊 Subscribed to device downlink channel: {channel}"
            f"{' (opus streaming)' if opus_stream else ''}"
        )

        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if not message or message["type"] != "message":
                    continue

                try:
                    payload = json.loads(message["data"])
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid device downlink message on {channel}: {e}")
                    continue

                msg_type = payload.get("type")
                if not msg_type:
                    logger.warning(f"Device downlink message missing 'type': {payload}")
                    continue

                try:
                    # Skip if the device socket already closed (a downlink can race the
                    # disconnect); sending after close raises the ASGI websocket.send error.
                    if websocket.client_state != WebSocketState.CONNECTED:
                        break
                    if msg_type == "stop-audio":
                        # Barge-in: stop whatever TTS is playing on the device. For
                        # Opus clients this cancels the in-flight stream + flushes the
                        # device; for others there's nothing streaming to cancel, so
                        # just forward the control frame best-effort.
                        if opus_stream:
                            await stop_play_audio(websocket, client_id_value)
                        else:
                            await websocket.send_json(payload)
                    elif opus_stream and msg_type == "play-audio":
                        # RAM-limited devices can't take a big base64 WAV frame;
                        # transcode + stream it as small Opus packets instead.
                        streamed = await stream_play_audio_as_opus(
                            websocket, payload.get("data") or {}, client_id_value
                        )
                        if not streamed:
                            await websocket.send_json(payload)  # fallback
                    else:
                        await websocket.send_json(payload)
                    data = payload.get("data")
                    # Summarize so large fields (e.g. base64 TTS audio) don't flood logs.
                    summary = (
                        {
                            k: (
                                f"<{len(v)} chars>"
                                if isinstance(v, str) and len(v) > 80
                                else v
                            )
                            for k, v in data.items()
                        }
                        if isinstance(data, dict)
                        else data
                    )
                    logger.info(
                        f"📤 Forwarded '{msg_type}' to device {client_id}: {summary}"
                    )
                except Exception as send_error:
                    logger.warning(
                        f"Failed to send downlink to device {client_id}: {send_error}"
                    )
                    break

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info(f"Device downlink subscriber cancelled for {client_id}")
                break
            except Exception as e:
                logger.error(
                    f"Error in device downlink subscriber for {client_id}: {e}",
                    exc_info=True,
                )
                break

    except Exception as e:
        logger.error(
            f"Failed to initialize device downlink subscriber for {client_id}: {e}",
            exc_info=True,
        )
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up downlink subscriber: {cleanup_error}")
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass


async def receive_with_idle_timeout(ws: WebSocket) -> dict:
    """`ws.receive()` with a liveness deadline.

    A live device streams audio every ~0.25s, so if nothing arrives for
    WS_IDLE_TIMEOUT_SECS the peer is dead (or a relay is holding the socket open
    after its device vanished). We raise WebSocketDisconnect so the handler's
    `finally` runs the same full cleanup as a clean close, reaping the zombie.
    """
    try:
        return dict(await asyncio.wait_for(ws.receive(), timeout=WS_IDLE_TIMEOUT_SECS))
    except asyncio.TimeoutError:
        logger.warning(
            f"⏰ WebSocket idle for {WS_IDLE_TIMEOUT_SECS:.0f}s with no data — "
            f"treating peer as disconnected"
        )
        raise WebSocketDisconnect(code=1001, reason="idle timeout")


async def parse_wyoming_protocol(ws: WebSocket) -> tuple[dict, Optional[bytes]]:
    """Parse Wyoming protocol: JSON header line followed by optional binary payload.

    Returns:
        Tuple of (header_dict, payload_bytes or None)
    """
    # Read data from WebSocket
    logger.debug(f"parse_wyoming_protocol: About to call ws.receive()")
    message = await receive_with_idle_timeout(ws)
    logger.debug(
        f"parse_wyoming_protocol: Received message with keys: {message.keys() if message else 'None'}"
    )

    # Handle WebSocket close frame
    if "type" in message and message["type"] == "websocket.disconnect":
        # This is a normal WebSocket close event
        code = message.get("code")
        reason = message.get("reason", "")
        logger.info(
            f"📴 WebSocket disconnect received in parse_wyoming_protocol. Code: {code}, Reason: {reason}"
        )
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
            payload_msg = await receive_with_idle_timeout(ws)
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
    """Create and register a new client state.

    If a connection with the same client_id already exists (a reconnecting device
    whose previous connection is still lingering — e.g. a relay-held zombie), the
    stale connection is evicted first so the newest connection always wins. The
    per-client lock makes evict+create atomic against concurrent reconnects.
    """
    # Get client manager
    client_manager = get_client_manager()

    async with _get_client_setup_lock(client_id):
        # Newest-wins: tear down any stale connection holding this client_id. This
        # finalizes its sessions and removes it so create_client below won't raise.
        if client_manager.has_client(client_id):
            logger.warning(
                f"♻️ Client {client_id} reconnecting while a previous connection is "
                f"still registered — evicting the stale connection (newest wins)"
            )
            await cleanup_client_state(client_id)

        # Use ClientManager for atomic client creation and registration
        client_state = client_manager.create_client(client_id, user.user_id, user.email)

    # Also track in persistent mapping (for database queries + cross-container Redis)
    await track_client_user_relationship_async(client_id, user.user_id)

    # Register client in user model (persistent)
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
    logger.info(
        f"🔄 Letting speech detection job complete naturally for client {client_id} (if running)"
    )

    client_manager = get_client_manager()
    client_state = client_manager.get_client(client_id)
    session_id = client_state.stream_session_id if client_state else None

    # Finalize only the recording owned by this connection. Historical sessions for
    # the same device are immutable and must never be rewritten on a reconnect.
    async_redis = create_async_redis(decode_responses=False)
    try:
        if session_id:
            store = SessionStore(async_redis)
            view = await store.read(session_id)
            if view is None:
                raise RuntimeError(
                    f"Connected client {client_id} references missing session {session_id}"
                )
            if view.client_id != client_id:
                raise RuntimeError(
                    f"Session {session_id} belongs to {view.client_id}, not {client_id}"
                )

            if view.status is SessionStatus.ACTIVE:
                logger.info(
                    f"📊 Finalizing active session {session_id[:12]} due to WebSocket disconnect"
                )
                await get_audio_stream_producer().finalize_session(
                    session_id, completion_reason="websocket_disconnect"
                )

            if view.status is not SessionStatus.FINISHED:
                await store.mark_complete(session_id, "websocket_disconnect")

            if view.user_id:
                await publish_sse_event_async(
                    view.user_id,
                    "session.ended",
                    {
                        "session_id": session_id,
                        "client_id": client_id,
                        "reason": "websocket_disconnect",
                    },
                )

            stream_name = view.stream_name
            if stream_name and await async_redis.exists(stream_name):
                await async_redis.persist(stream_name)
                decision = await inspect_stream_retention(
                    async_redis,
                    stream_name,
                    required_groups={AUDIO_PERSISTENCE_GROUP},
                )
                logger.info(
                    f"Retained draining Redis audio log {stream_name}: {decision.reason}"
                )

            results_key = f"transcription:results:{session_id}"
            if await async_redis.exists(results_key):
                await async_redis.expire(results_key, 300)
            await store.expire_current_conversation(session_id, 3600)
            await async_redis.expire(f"speech_detection_job:{session_id}", 3600)
    finally:
        await async_redis.aclose()

    # Client removal is legal only after its session reached the producer-terminal
    # state. A failed Redis append raises above and retains both ClientState and the
    # producer buffer for the next explicit cleanup attempt.
    removed = await client_manager.remove_client_with_cleanup(client_id)

    if removed:
        logger.info(f"Client {client_id} cleaned up successfully")
    else:
        logger.warning(f"Client {client_id} was not found for cleanup")

    # Stamp the device's last_seen in the registry so the Network page shows an
    # accurate "last seen" once it's offline (the live ClientState is now gone).
    try:
        await touch_client_last_seen(client_id)
    except Exception as e:
        logger.debug(f"Could not stamp last_seen for {client_id}: {e}")


# Shared helper functions for WebSocket handlers
async def _setup_websocket_connection(
    ws: WebSocket,
    token: Optional[str],
    device_name: Optional[str],
    pending_client_id: str,
    connection_type: str,
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
    user, auth_failure_reason = await websocket_auth(ws, token)
    if not user:
        # Build specific error message based on failure reason
        if auth_failure_reason == "token_expired":
            error_type = "token_expired"
            message = "Your session has expired. Please log in again."
            close_reason = "Token expired"
        elif auth_failure_reason == "user_not_found":
            error_type = "user_not_found"
            message = "User account not found or inactive. Please log in again."
            close_reason = "User not found"
        else:
            error_type = "authentication_failed"
            message = "Authentication failed. Please log in again and ensure your token is valid."
            close_reason = "Authentication failed"

        # Send error message to client before closing
        try:
            error_msg = (
                json.dumps(
                    {
                        "type": "error",
                        "error": error_type,
                        "message": message,
                        "code": 1008,
                    }
                )
                + "\n"
            )
            await ws.send_text(error_msg)
            application_logger.info(
                f"Sent auth error to client: {error_type} ({close_reason})"
            )
        except Exception as send_error:
            application_logger.warning(f"Failed to send error message: {send_error}")

        # Close connection with appropriate code
        await ws.close(code=1008, reason=close_reason)
        return None, None, None

    # Generate proper client_id using user and device_name
    client_id = generate_client_id(user, device_name)

    # Remove from pending now that we have real client_id
    pending_connections.discard(pending_client_id)
    application_logger.info(
        f"🔌 {connection_type} WebSocket connection accepted - User: {user.user_id} ({user.email}), Client: {client_id}"
    )

    # Send ready message to confirm connection is established
    try:
        ready_msg = (
            json.dumps(
                {
                    "type": "ready",
                    "message": "WebSocket connection established",
                    # The resolved client_id lets the client scope per-client signals
                    # (wake-word SSE feedback) to its own device — see webui useSSE.
                    "client_id": client_id,
                }
            )
            + "\n"
        )
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
    websocket: Optional[WebSocket] = None,
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

    if client_state.stream_session_id is not None:
        application_logger.debug(f"Session already initialized for {client_id}")
        return None

    # Determine transcription provider from config.yml
    registry = get_models_registry()
    if not registry:
        raise ValueError(
            "config.yml not found - cannot determine transcription provider"
        )

    stt_model = registry.get_default("stt")
    if not stt_model:
        raise ValueError("No default STT model configured in config.yml (defaults.stt)")

    # Use model_provider for session tracking (generic, not validated against hardcoded list)
    provider = (
        stt_model.model_provider.lower() if stt_model.model_provider else stt_model.name
    )

    application_logger.info(
        f"📋 Using STT provider: {provider} (model: {stt_model.name})"
    )

    # Every recording attempt owns a distinct session and raw-audio WAL. Reusing
    # client_id here lets a reconnect reset the old session hash and interleave new
    # bytes with a persistence worker that is still draining the prior connection.
    session_id = f"{client_id}-{uuid.uuid4().hex}"
    application_logger.info(f"🆔 Creating stream session: {session_id}")

    # Initialize session tracking in Redis (SINGLE SOURCE OF TRUTH for session metadata)
    # This includes user_email, connection info, audio format, chunk counters, job IDs, etc.
    connection_id = f"ws_{client_id}_{int(time.time())}"
    await audio_stream_producer.init_session(
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        user_email=user_email,
        connection_id=connection_id,
        mode="streaming",
        provider=provider,
    )
    # Publish the connection's active-session pointer only after the durable Redis
    # session and producer buffer both exist. A failed CONNECTING attempt therefore
    # cannot masquerade as an active capture during cleanup/reconnect.
    client_state.stream_session_id = session_id

    # Store audio format in Redis session (not in ClientState)
    await audio_stream_producer.store.set_audio_format(session_id, audio_format)

    # CONNECTED/ACTIVE is not allowed to accept raw audio without a durable Mongo
    # owner. Create and atomically assign it before the persistence job or producer
    # can observe the session. Speech detection may reuse/finalize this placeholder;
    # it no longer controls whether the raw capture exists.
    assignment = await ensure_active_session_placeholder(
        audio_stream_producer.store,
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
    )
    if assignment is None:
        raise RuntimeError(
            f"Could not assign durable audio owner for {client_state.stream_session_id}"
        )

    # Enqueue streaming jobs (speech detection + audio persistence). RQ's client is
    # synchronous, so every enqueue and liveness check is a blocking Redis round-trip.
    job_ids = await asyncio.to_thread(
        start_streaming_jobs,
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
    )

    # Store job IDs in Redis session (not in ClientState)
    await audio_stream_producer.update_session_job_ids(
        session_id=session_id,
        speech_detection_job_id=job_ids["speech_detection"],
        audio_persistence_job_id=job_ids["audio_persistence"],
    )

    # Notify frontend that a new streaming session has started
    await publish_sse_event_async(
        user_id,
        "session.started",
        {
            "session_id": session_id,
            "client_id": client_id,
        },
    )

    # The durable placeholder was assigned synchronously above; the worker only
    # consumes that state and never invents an alternate owner.

    # Launch interim results subscriber if WebSocket provided
    subscriber_task = None
    if websocket:
        subscriber_task = asyncio.create_task(
            subscribe_to_interim_results(websocket, session_id)
        )
        application_logger.info(
            f"📡 Launched interim results subscriber for session {session_id}"
        )

    return subscriber_task


async def _finalize_streaming_session(
    client_state, audio_stream_producer, user_id: str, user_email: str, client_id: str
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
    if client_state.stream_session_id is None:
        application_logger.debug(f"No active session to finalize for {client_id}")
        return

    session_id = client_state.stream_session_id

    # AudioStreamProducer owns the ordered durability transition: flush its
    # process buffer, append the terminal marker, then publish FINALIZING.  Do not
    # split those steps across this controller and the disconnect path.
    await audio_stream_producer.finalize_session(
        session_id, completion_reason="user_stopped"
    )

    # Store markers in Redis so open_conversation_job can persist them
    if client_state.markers:
        await audio_stream_producer.store.set_markers(session_id, client_state.markers)
        client_state.markers.clear()

    # Producer completion is distinct from persistence completion: FINISHED means no
    # more XADDs are legal, while Redis group lag/pending remains the deletion gate.
    await audio_stream_producer.store.mark_complete(session_id, "user_stopped")
    await publish_sse_event_async(
        user_id,
        "session.ended",
        {
            "session_id": session_id,
            "client_id": client_id,
            "reason": "user_stopped",
        },
    )

    application_logger.info(
        f"✅ Session {session_id[:12]} producer closed; persistence is draining"
    )

    # Clear the connection pointer only after the producer completed the durable
    # transition. On error the caller sees the exception and the same session/buffer
    # remains available for an explicit retry; silently starting a fresh session
    # would overwrite the only copy of the final sub-chunk.
    client_state.stream_session_id = None
    client_state.last_persistence_healthcheck = 0.0


async def _publish_audio_to_stream(
    client_state,
    audio_stream_producer,
    audio_data: bytes,
    user_id: str,
    client_id: str,
    sample_rate: int,
    channels: int,
    sample_width: int,
    captured_at: float | None = None,
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
    if client_state.stream_session_id is None:
        raise RuntimeError(
            f"Received audio chunk before streaming session initialization for {client_id}"
        )

    session_id = client_state.stream_session_id

    # Publish to Redis Stream using producer (producer owns chunk counting)
    await audio_stream_producer.add_audio_chunk(
        audio_data=audio_data,
        session_id=session_id,
        user_id=user_id,
        client_id=client_id,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        captured_at=captured_at,
    )


async def _handle_omi_audio_chunk(
    client_state,
    audio_stream_producer,
    opus_payload: bytes,
    decode_packet_fn,
    user_id: str,
    client_id: str,
    packet_count: int,
    captured_at: float | None = None,
) -> bool:
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
    pcm_data = await loop.run_in_executor(
        _DEC_IO_EXECUTOR, decode_packet_fn, opus_payload
    )
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
            OMI_SAMPLE_WIDTH,
            captured_at,
        )
        return True
    else:
        # Log decode failures for first 5 packets
        if packet_count <= 5:
            application_logger.warning(
                f"❌ Failed to decode OMI packet #{packet_count}: {len(opus_payload)} bytes"
            )
        return False


async def _handle_streaming_mode_audio(
    client_state,
    audio_stream_producer,
    audio_data: bytes,
    audio_format: dict,
    user_id: str,
    user_email: str,
    client_id: str,
    websocket: Optional[WebSocket] = None,
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
    if client_state.stream_session_id is None:
        subscriber_task = await _initialize_streaming_session(
            client_state,
            audio_stream_producer,
            user_id,
            user_email,
            client_id,
            audio_format,
            websocket=websocket,  # Pass WebSocket to launch interim results subscriber
        )

    # Streaming transcription and Mongo persistence consume the Redis audio stream
    # independently. Verify persistence periodically so an RQ crash/early exit cannot
    # leave ASR apparently healthy while durable audio silently disappears. Messages
    # remain in the Redis stream until the replacement consumer drains them.
    now = time.monotonic()
    if now - client_state.last_persistence_healthcheck >= 1.0:
        session_id = client_state.stream_session_id
        if session_id is None:
            raise RuntimeError(
                "Streaming session initialization did not produce a session id"
            )
        # Once per second per streaming client, and several synchronous Redis
        # round-trips deep. A reconnect inside it blocks on getaddrinfo.
        await asyncio.to_thread(
            ensure_audio_persistence, session_id, user_id, client_id
        )
        client_state.last_persistence_healthcheck = now

    # Publish to Redis Stream
    await _publish_audio_to_stream(
        client_state,
        audio_stream_producer,
        audio_data,
        user_id,
        client_id,
        audio_format.get("rate", 16000),
        audio_format.get("channels", 1),
        audio_format.get("width", 2),
    )

    return subscriber_task


async def _handle_batch_mode_audio(
    client_state, audio_data: bytes, audio_format: dict, client_id: str
) -> None:
    """
    Handle audio chunk in batch mode with rolling 30-minute limit.

    Args:
        client_state: Client state object
        audio_data: Raw PCM audio bytes
        audio_format: Audio format dict
        client_id: Client ID
    """
    # Capture the audio format on the first chunk of the connection's first batch
    if not client_state.batch_started:
        client_state.batch_started = True
        client_state.batch_audio_format = audio_format
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
            batch_number=client_state.batch_chunks_processed + 1,
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
    websocket: Optional[WebSocket] = None,
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
    recording_mode = client_state.recording_mode

    if recording_mode == "streaming":
        return await _handle_streaming_mode_audio(
            client_state,
            audio_stream_producer,
            audio_data,
            audio_format,
            user_id,
            user_email,
            client_id,
            websocket=websocket,
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
    websocket: Optional[WebSocket] = None,
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
                        "code": 400,
                    }
                    await websocket.send_json(error_response)
                    application_logger.info(
                        f"📤 Sent streaming error to WebUI client {client_id}"
                    )

                    # Close the websocket connection after sending error
                    await websocket.close(
                        code=1008, reason="Streaming transcription not configured"
                    )
                    application_logger.info(
                        f"🔌 Closed WebSocket connection for {client_id} due to streaming config error"
                    )

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
    client_state, audio_stream_producer, user_id: str, user_email: str, client_id: str
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
    recording_mode = client_state.recording_mode
    application_logger.info(
        f"🛑 Audio session stopped for {client_id} (mode: {recording_mode})"
    )

    if recording_mode == "streaming":
        await _finalize_streaming_session(
            client_state, audio_stream_producer, user_id, user_email, client_id
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
        button_state: Button state string (e.g., "SINGLE_PRESS", "DOUBLE_PRESS")
        user_id: User ID
        client_id: Client ID
    """
    timestamp = time.time()
    # The live conversation id is assigned later in the RQ pipeline and is not
    # tracked on ClientState; markers carry the session id instead.
    session_id = client_state.stream_session_id

    application_logger.info(
        f"🔘 Button event from {client_id}: {button_state} "
        f"(session_id={session_id})"
    )

    # Store marker on client state for later persistence to conversation
    marker = {
        "type": "button_event",
        "state": button_state,
        "timestamp": timestamp,
        "audio_uuid": None,  # assigned later in the pipeline, not on ClientState
        "session_id": session_id,
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
                "audio_uuid": None,  # assigned later in the pipeline, not on ClientState
                "session_id": session_id,
                "client_id": client_id,
            },
        )


async def _handle_dial_event(
    client_state,
    direction: str,
    user_id: str,
    client_id: str,
) -> None:
    """Handle a rotary-dial event (CW/CCW) from the device.

    During an open wake follow-up window, a detent is a *physical* follow-up that
    nudges the just-controlled lights (warmer/cooler or brighter/dimmer). Outside a
    window it's a no-op here — the device may still use the dial locally (e.g. for
    volume). Best-effort: never breaks the audio loop.
    """
    session_id = client_state.stream_session_id
    router = get_plugin_router()
    if not router or not session_id:
        return

    redis_client = create_async_redis(decode_responses=True)
    try:
        result = await handle_dial_followup(
            redis_client,
            router,
            user_id=user_id,
            session_id=session_id,
            client_id=client_id,
            direction=direction,
        )
        application_logger.info(
            f"🎛️ Dial event from {client_id}: {direction} (session={session_id}) -> {result}"
        )
    except Exception as e:  # noqa: BLE001 - dial feedback must never break the loop
        application_logger.warning(f"Dial event handling failed for {client_id}: {e}")
    finally:
        await redis_client.aclose()


async def _create_batch_conversation_and_enqueue(
    client_state,
    user_id: str,
    client_id: str,
    title: str,
    trigger: str,
    job_id_prefix: str,
    enqueue_post_jobs: bool = False,
    attach_markers: bool = False,
) -> Optional[str]:
    """Create conversation from batch audio, store chunks, enqueue transcription.

    Args:
        client_state: Client state with batch_audio_chunks
        user_id: User ID
        client_id: Client ID
        title: Conversation title
        trigger: Trigger string for transcription job
        job_id_prefix: Prefix for the transcription job ID
        enqueue_post_jobs: If True, chain post-conversation jobs after transcription
        attach_markers: If True, copy client_state.markers to conversation

    Returns:
        conversation_id on success, None on failure.
    """
    complete_audio = b"".join(client_state.batch_audio_chunks)
    audio_format = client_state.batch_audio_format
    sample_rate = audio_format.get("rate", 16000)
    sample_width = audio_format.get("width", 2)
    channels = audio_format.get("channels", 1)

    application_logger.info(
        f"📦 Batch: Combined {len(client_state.batch_audio_chunks)} chunks "
        f"into {len(complete_audio)} bytes (title={title})"
    )

    # Create conversation
    conversation = create_conversation(
        user_id=user_id,
        client_id=client_id,
        title=title,
        summary="Processing batch audio...",
    )
    if attach_markers and client_state.markers:
        conversation.markers = list(client_state.markers)
        client_state.markers.clear()
    await conversation.insert()
    conversation_id = conversation.conversation_id

    # Convert audio to MongoDB chunks
    try:
        num_chunks = await convert_audio_to_chunks(
            conversation_id=conversation_id,
            audio_data=complete_audio,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )
        application_logger.info(
            f"📦 Batch: Converted to {num_chunks} MongoDB chunks ({conversation_id[:12]})"
        )
    except Exception as chunk_error:
        application_logger.error(
            f"Failed to convert batch audio to chunks: {chunk_error}", exc_info=True
        )

    # Enqueue transcription job
    version_id = str(uuid.uuid4())
    transcription_job = transcription_queue.enqueue(
        transcribe_full_audio_job,
        conversation_id,
        version_id,
        trigger,
        job_timeout=-1,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"{job_id_prefix}_{conversation_id[:12]}",
        description=f"Transcribe {title.lower()} {conversation_id[:8]}",
        meta={"conversation_id": conversation_id, "client_id": client_id},
    )

    application_logger.info(
        f"📥 Batch: Enqueued transcription job {transcription_job.id}"
    )

    # Optionally chain post-conversation jobs
    if enqueue_post_jobs:
        job_ids = start_post_conversation_jobs(
            conversation_id=conversation_id,
            user_id=None,
            depends_on_job=transcription_job,
            client_id=client_id,
        )
        application_logger.info(
            f"✅ Batch: Enqueued job chain for {conversation_id} — "
            f"transcription ({transcription_job.id}) → "
            f"speaker ({job_ids['speaker_recognition']}) → "
            f"memory ({job_ids['memory']})"
        )

    return conversation_id


async def _process_rolling_batch(
    client_state, user_id: str, user_email: str, client_id: str, batch_number: int
) -> None:
    """Process accumulated batch audio as a rolling segment."""
    if not client_state.batch_audio_chunks:
        application_logger.warning(f"⚠️ No audio chunks to process for rolling batch")
        return

    try:
        await _create_batch_conversation_and_enqueue(
            client_state,
            user_id=user_id,
            client_id=client_id,
            title=TITLE_NOT_GENERATED,
            trigger=f"rolling_batch_{batch_number}",
            job_id_prefix=f"transcribe_rolling_{batch_number}",
        )
    except Exception as e:
        application_logger.error(
            f"❌ Failed to process rolling batch #{batch_number}: {e}", exc_info=True
        )


async def _process_batch_audio_complete(
    client_state, user_id: str, user_email: str, client_id: str
) -> None:
    """Process completed batch audio: create conversation, enqueue full job chain."""
    if not client_state.batch_audio_chunks:
        application_logger.warning(
            f"⚠️ Batch mode: No audio chunks accumulated for {client_id}"
        )
        return

    try:
        await _create_batch_conversation_and_enqueue(
            client_state,
            user_id=user_id,
            client_id=client_id,
            title=TITLE_NOT_GENERATED,
            trigger="batch",
            job_id_prefix="transcribe",
            enqueue_post_jobs=True,
            attach_markers=True,
        )
        client_state.batch_audio_chunks = []
    except Exception as batch_error:
        application_logger.error(
            f"❌ Batch mode processing failed: {batch_error}", exc_info=True
        )


async def _cleanup_websocket_connection(
    client_id: Optional[str],
    pending_client_id: str,
    interim_subscriber_task: Optional[asyncio.Task],
) -> None:
    """
    Shared cleanup for WebSocket handlers (OMI and PCM).

    Cancels the interim results subscriber, removes the pending connection
    tracking entry, and tears down client state.

    Args:
        client_id: Actual client ID (may be None if auth failed)
        pending_client_id: Temporary tracking ID to discard
        interim_subscriber_task: Background task forwarding interim transcripts
    """
    # Cancel interim results subscriber task if running
    if interim_subscriber_task and not interim_subscriber_task.done():
        interim_subscriber_task.cancel()
        try:
            await interim_subscriber_task
        except asyncio.CancelledError:
            application_logger.info(
                f"Interim subscriber task cancelled for {client_id}"
            )
        except Exception as task_error:
            application_logger.error(
                f"Error cancelling interim subscriber task: {task_error}"
            )

    # Clean up pending connection tracking
    pending_connections.discard(pending_client_id)

    # Ensure cleanup happens even if client_id is None
    if client_id:
        try:
            await cleanup_client_state(client_id)
        except Exception as cleanup_error:
            application_logger.error(
                f"Error during cleanup for client {client_id}: {cleanup_error}",
                exc_info=True,
            )


from contextlib import asynccontextmanager


@asynccontextmanager
async def _websocket_session(ws, token, device_name, connection_type):
    """Lifecycle wrapper: pending tracking, auth, client setup, cleanup.

    Yields (client_id, client_state, user, audio_stream_producer, interim_holder)
    on success, or None if auth failed.
    interim_holder is a mutable list — the inner loop sets interim_holder[0] = task.
    """
    pending_client_id = f"pending_{uuid.uuid4()}"
    pending_connections.add(pending_client_id)

    client_id = None
    interim_holder = [None]  # mutable so inner loop can update
    downlink_task = None

    try:
        client_id, client_state, user = await _setup_websocket_connection(
            ws, token, device_name, pending_client_id, connection_type
        )
        if not user:
            yield None
            return

        # user_id / user_email / client_id are already set on client_state by
        # create_client_state() during connection setup.
        audio_stream_producer = get_audio_stream_producer()

        # Forward backend→device control messages (tones, TTS) for this device.
        downlink_task = asyncio.create_task(
            subscribe_to_device_downlink(ws, ClientId.from_value(client_id))
        )

        yield (client_id, client_state, user, audio_stream_producer, interim_holder)

    except WebSocketDisconnect:
        application_logger.info(
            f"🔌 {connection_type} WebSocket disconnected — Client: {client_id}"
        )
    except Exception as e:
        application_logger.error(
            f"❌ {connection_type} WebSocket error for client {client_id}: {e}",
            exc_info=True,
        )
        # Surface error-disconnects as a first-class client event (the catch-all log
        # handler also captures the line above, but without client_id / category).
        record_event_sync(
            severity="error",
            category="client",
            source=client_id or "unknown",
            title=f"{connection_type} client disconnected on error",
            detail=str(e),
            traceback=traceback.format_exc(),
            client_id=client_id,
            metadata={"connection_type": connection_type},
        )
    finally:
        if downlink_task and not downlink_task.done():
            downlink_task.cancel()
            try:
                await downlink_task
            except asyncio.CancelledError:
                pass
            except Exception as task_error:
                application_logger.error(
                    f"Error cancelling downlink task for {client_id}: {task_error}"
                )
        await _cleanup_websocket_connection(
            client_id, pending_client_id, interim_holder[0]
        )


async def handle_omi_websocket(
    ws: WebSocket,
    token: Optional[str] = None,
    device_name: Optional[str] = None,
):
    """Handle OMI WebSocket connections with Opus decoding."""
    async with _websocket_session(ws, token, device_name, "OMI") as session:
        if session is None:
            return
        client_id, client_state, user, audio_stream_producer, interim_holder = session

        # OMI-specific: Setup Opus decoder
        decoder = OmiOpusDecoder()
        _decode_packet = partial(decoder.decode_packet, strip_header=False)

        packet_count = 0
        total_bytes = 0

        while True:
            # Parse Wyoming protocol
            header, payload = await parse_wyoming_protocol(ws)
            client_state.touch()  # liveness: inbound activity keeps this client fresh

            if header["type"] == "audio-start":
                application_logger.info(
                    f"🔴 BACKEND: Received audio-start in OMI MODE for {client_id} (header={header})"
                )
                application_logger.info(f"🎙️ OMI audio session started for {client_id}")

                interim_holder[0] = await _initialize_streaming_session(
                    client_state,
                    audio_stream_producer,
                    user.user_id,
                    user.email,
                    client_id,
                    header.get(
                        "data",
                        {
                            "rate": OMI_SAMPLE_RATE,
                            "width": OMI_SAMPLE_WIDTH,
                            "channels": OMI_CHANNELS,
                        },
                    ),
                    websocket=ws,
                )

            elif header["type"] == "audio-chunk" and payload:
                chunk_data = header.get("data", {})
                # The mobile spool's own file identity, NOT this connection's
                # SessionId. It was sent as ``durable_session_id`` and acknowledged as
                # ``session_id``, so a spool-segment id and the backend audio session
                # shared a name while meaning different things.
                spool_segment_id = chunk_data.get("spool_segment_id")
                spool_sequence = chunk_data.get("spool_sequence")
                receipt_key = None
                if spool_segment_id is not None and spool_sequence is not None:
                    receipt_key = (
                        f"mobile-audio-receipt:{user.user_id}:{client_id}:"
                        f"{spool_segment_id}"
                    )
                    prior = await audio_stream_producer.redis_client.get(receipt_key)
                    if prior is not None and int(prior) >= int(spool_sequence):
                        await ws.send_json(
                            {
                                "type": "audio-ack",
                                "spool_segment_id": spool_segment_id,
                                "sequence": int(spool_sequence),
                            }
                        )
                        continue
                packet_count += 1
                total_bytes += len(payload)

                if packet_count <= 5 or packet_count % 1000 == 0:
                    application_logger.info(
                        f"🎵 Received OMI audio chunk #{packet_count}: {len(payload)} bytes"
                    )

                decoded = await _handle_omi_audio_chunk(
                    client_state,
                    audio_stream_producer,
                    payload,
                    _decode_packet,
                    user.user_id,
                    client_id,
                    packet_count,
                    (
                        float(chunk_data["captured_at_ms"]) / 1000.0
                        if chunk_data.get("captured_at_ms") is not None
                        else None
                    ),
                )

                if receipt_key is not None and decoded:
                    # ACK only after the decoded bytes cross the Redis WAL boundary.
                    await audio_stream_producer.flush_session_buffer(
                        client_state.stream_session_id,
                        sample_rate=OMI_SAMPLE_RATE,
                        channels=OMI_CHANNELS,
                        sample_width=OMI_SAMPLE_WIDTH,
                    )
                    await audio_stream_producer.redis_client.set(
                        receipt_key, int(spool_sequence), ex=7 * 24 * 60 * 60
                    )
                    await ws.send_json(
                        {
                            "type": "audio-ack",
                            "spool_segment_id": spool_segment_id,
                            "sequence": int(spool_sequence),
                        }
                    )

                if packet_count % 1000 == 0:
                    application_logger.info(
                        f"📊 Processed {packet_count} OMI packets ({total_bytes} bytes total)"
                    )

            elif header["type"] == "audio-stop":
                application_logger.info(
                    f"🛑 OMI audio session stopped for {client_id} - "
                    f"Total chunks: {packet_count}, Total bytes: {total_bytes}"
                )

                await _finalize_streaming_session(
                    client_state,
                    audio_stream_producer,
                    user.user_id,
                    user.email,
                    client_id,
                )

                packet_count = 0
                total_bytes = 0

            elif header["type"] == "ping":
                # App-level heartbeat. The mobile client (useAudioStreamer) pings every
                # 25s and closes the socket as a half-open "zombie" if it gets no pong
                # within 2 heartbeats (~50s). The OMI/opus path previously had no pong
                # reply (only the PCM handler did), so opus clients dropped + reconnected
                # every ~50s — churning the streaming-transcription provider connection
                # and stranding real speech as "transcription service did not respond".
                application_logger.debug(
                    f"🏓 Received ping from OMI client {client_id}"
                )
                await ws.send_json({"type": "pong"})

            elif header["type"] == "button-event":
                button_data = header.get("data", {})
                button_state = button_data.get("state", "unknown")
                await _handle_button_event(
                    client_state, button_state, user.user_id, client_id
                )

            elif header["type"] == "dial-event":
                dial_data = header.get("data", {})
                direction = dial_data.get("direction", "")
                await _handle_dial_event(
                    client_state, direction, user.user_id, client_id
                )

            else:
                application_logger.debug(
                    f"Ignoring Wyoming event type '{header['type']}' for OMI client {client_id}"
                )


async def handle_pcm_websocket(
    ws: WebSocket, token: Optional[str] = None, device_name: Optional[str] = None
):
    """Handle PCM WebSocket connections with batch and streaming mode support."""
    async with _websocket_session(ws, token, device_name, "PCM") as session:
        if session is None:
            return
        client_id, client_state, user, audio_stream_producer, interim_holder = session

        packet_count = 0
        total_bytes = 0
        audio_streaming = False

        while True:
            try:
                if not audio_streaming:
                    # Control message mode - parse Wyoming protocol
                    application_logger.debug(
                        f"🔄 Control mode for {client_id}, WebSocket state: {ws.client_state if hasattr(ws, 'client_state') else 'unknown'}"
                    )
                    application_logger.debug(
                        f"📨 About to receive control message for {client_id}"
                    )
                    header, payload = await parse_wyoming_protocol(ws)
                    client_state.touch()  # liveness: inbound activity keeps this client fresh
                    application_logger.debug(
                        f"✅ Received message type: {header.get('type')} for {client_id}"
                    )

                    if header["type"] == "audio-start":
                        application_logger.info(
                            f"🔴 BACKEND: Received audio-start in CONTROL MODE for {client_id}"
                        )
                        application_logger.debug(
                            f"🎙️ Processing audio-start for {client_id}"
                        )

                        # Handle audio session start (pass websocket for error handling)
                        audio_streaming, recording_mode = (
                            await _handle_audio_session_start(
                                client_state,
                                header.get("data", {}),
                                client_id,
                                websocket=ws,
                            )
                        )

                        # Initialize streaming session
                        if recording_mode == "streaming":
                            application_logger.info(
                                f"🔴 BACKEND: Initializing streaming session for {client_id}"
                            )
                            interim_holder[0] = await _initialize_streaming_session(
                                client_state,
                                audio_stream_producer,
                                user.user_id,
                                user.email,
                                client_id,
                                header.get("data", {}),
                                websocket=ws,
                            )

                        continue

                    elif header["type"] == "ping":
                        application_logger.debug(f"🏓 Received ping from {client_id}")
                        # Reply so the client can detect a half-open (zombie) socket.
                        await ws.send_json({"type": "pong"})
                        continue

                    elif header["type"] == "button-event":
                        button_data = header.get("data", {})
                        button_state = button_data.get("state", "unknown")
                        await _handle_button_event(
                            client_state, button_state, user.user_id, client_id
                        )
                        continue

                    elif header["type"] == "dial-event":
                        dial_data = header.get("data", {})
                        direction = dial_data.get("direction", "")
                        await _handle_dial_event(
                            client_state, direction, user.user_id, client_id
                        )
                        continue

                    else:
                        application_logger.debug(
                            f"Ignoring Wyoming control event type '{header['type']}' for {client_id}"
                        )
                        continue

                else:
                    # Audio streaming mode
                    application_logger.debug(
                        f"🎵 Audio streaming mode for {client_id} - waiting for audio data"
                    )

                    try:
                        message = await receive_with_idle_timeout(ws)
                        client_state.touch()  # liveness: inbound activity keeps this client fresh

                        if (
                            "type" in message
                            and message["type"] == "websocket.disconnect"
                        ):
                            code = message.get("code")
                            reason = message.get("reason", "")
                            application_logger.info(
                                f"🔌 WebSocket disconnect during audio streaming for {client_id}. Code: {code}, Reason: {reason}"
                            )
                            break

                        if "text" in message:
                            try:
                                control_header = json.loads(message["text"].strip())
                                if control_header.get("type") == "audio-stop":
                                    audio_streaming = await _handle_audio_session_stop(
                                        client_state,
                                        audio_stream_producer,
                                        user.user_id,
                                        user.email,
                                        client_id,
                                    )
                                    packet_count = 0
                                    total_bytes = 0
                                    continue
                                elif control_header.get("type") == "ping":
                                    application_logger.debug(
                                        f"🏓 Received ping during streaming from {client_id}"
                                    )
                                    # Reply so the client can detect a half-open (zombie) socket.
                                    await ws.send_json({"type": "pong"})
                                    continue
                                elif control_header.get("type") == "audio-start":
                                    application_logger.info(
                                        f"🔄 Ignoring duplicate audio-start message during streaming for {client_id}"
                                    )
                                    continue
                                elif control_header.get("type") == "audio-chunk":
                                    payload_length = control_header.get(
                                        "payload_length"
                                    )
                                    if payload_length and payload_length > 0:
                                        payload_msg = await receive_with_idle_timeout(
                                            ws
                                        )
                                        if "bytes" in payload_msg:
                                            audio_data = payload_msg["bytes"]
                                            packet_count += 1
                                            total_bytes += len(audio_data)

                                            application_logger.debug(
                                                f"🎵 Received audio chunk #{packet_count}: {len(audio_data)} bytes"
                                            )

                                            audio_format = control_header.get(
                                                "data", {}
                                            )
                                            task = await _handle_audio_chunk(
                                                client_state,
                                                audio_stream_producer,
                                                audio_data,
                                                audio_format,
                                                user.user_id,
                                                user.email,
                                                client_id,
                                                websocket=ws,
                                            )
                                            if task and not interim_holder[0]:
                                                interim_holder[0] = task
                                        else:
                                            application_logger.warning(
                                                f"Expected binary payload for audio-chunk, got: {payload_msg.keys()}"
                                            )
                                    else:
                                        application_logger.warning(
                                            f"audio-chunk missing payload_length: {payload_length}"
                                        )
                                    continue
                                elif control_header.get("type") == "button-event":
                                    button_data = control_header.get("data", {})
                                    button_state = button_data.get("state", "unknown")
                                    await _handle_button_event(
                                        client_state,
                                        button_state,
                                        user.user_id,
                                        client_id,
                                    )
                                    continue
                                elif control_header.get("type") == "dial-event":
                                    dial_data = control_header.get("data", {})
                                    direction = dial_data.get("direction", "")
                                    await _handle_dial_event(
                                        client_state,
                                        direction,
                                        user.user_id,
                                        client_id,
                                    )
                                    continue
                                else:
                                    application_logger.warning(
                                        f"Unknown control message during streaming: {control_header.get('type')}"
                                    )
                                    continue

                            except json.JSONDecodeError:
                                application_logger.warning(
                                    f"Invalid control message during streaming for {client_id}"
                                )
                                continue

                        elif "bytes" in message:
                            audio_data = message["bytes"]
                            packet_count += 1
                            total_bytes += len(audio_data)

                            application_logger.debug(
                                f"🎵 Received raw audio chunk #{packet_count}: {len(audio_data)} bytes"
                            )

                            default_format = {"rate": 16000, "width": 2, "channels": 1}
                            task = await _handle_audio_chunk(
                                client_state,
                                audio_stream_producer,
                                audio_data,
                                default_format,
                                user.user_id,
                                user.email,
                                client_id,
                                websocket=ws,
                            )
                            if task and not interim_holder[0]:
                                interim_holder[0] = task

                        else:
                            application_logger.warning(
                                f"Unexpected message format in streaming mode: {message.keys()}"
                            )
                            continue

                    except WebSocketDisconnect as disconnect:
                        # Expected: clean close, or an idle/zombie socket reaped by
                        # receive_with_idle_timeout. Not an error — exit so `finally`
                        # runs the normal cleanup. (WebSocketDisconnect subclasses
                        # Exception, so it must be caught before the generic handler.)
                        application_logger.info(
                            f"🔌 WebSocket disconnect during audio streaming for "
                            f"{client_id}. Code: {disconnect.code}, Reason: {disconnect.reason}"
                        )
                        break
                    except Exception as streaming_error:
                        application_logger.error(
                            f"Error in audio streaming mode for {client_id}: "
                            f"{type(streaming_error).__name__}: {streaming_error}",
                            exc_info=True,
                        )
                        # The protocol has no per-chunk replay ACK. Continuing after
                        # an ingress/finalization error would skip the failed packet
                        # and pretend the capture stayed contiguous. Stop accepting
                        # this connection; cleanup retries the same session transition.
                        break

            except WebSocketDisconnect as e:
                application_logger.info(
                    f"🔌 WebSocket disconnected during message processing for {client_id}. "
                    f"Code: {e.code}, Reason: {e.reason}"
                )
                break
            except json.JSONDecodeError as e:
                application_logger.error(
                    f"❌ JSON decode error in Wyoming protocol for {client_id}: {e}"
                )
                continue
            except ValueError as e:
                application_logger.error(f"❌ Protocol error for {client_id}: {e}")
                continue
            except RuntimeError as e:
                if "disconnect" in str(e).lower():
                    application_logger.info(
                        f"🔌 WebSocket already disconnected for {client_id}: {e}"
                    )
                    break
                else:
                    application_logger.error(
                        f"❌ Runtime error for {client_id}: {e}", exc_info=True
                    )
                    continue
            except Exception as e:
                application_logger.error(
                    f"❌ Unexpected error processing message for {client_id}: {e}",
                    exc_info=True,
                )
                error_msg = str(e).lower()
                if (
                    "disconnect" in error_msg
                    or "closed" in error_msg
                    or "receive" in error_msg
                ):
                    application_logger.info(
                        f"🔌 Connection issue detected for {client_id}, exiting loop"
                    )
                    break
                else:
                    continue
