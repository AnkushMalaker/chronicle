"""Transport-independent lifecycle for authenticated audio-v2 capture."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
import uuid

from advanced_omi_backend.client_manager import (
    get_client_manager,
    track_client_user_relationship_async,
)
from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.models.audio_capture import CaptureStartProvenance
from advanced_omi_backend.plugins.events import BUTTON_STATE_TO_EVENT, ButtonState
from advanced_omi_backend.redis_factory import create_async_redis
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
from advanced_omi_backend.services.plugin_service import get_plugin_router
from advanced_omi_backend.services.sse_publisher import publish_sse_event_async
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator
from advanced_omi_backend.users import register_client_to_user, touch_client_last_seen

logger = logging.getLogger(__name__)
application_logger = logging.getLogger("audio_processing")

DECODER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=os.cpu_count() or 4, thread_name_prefix="opus_io"
)
_client_setup_locks: dict[str, asyncio.Lock] = {}


def _client_setup_lock(client_id: str) -> asyncio.Lock:
    lock = _client_setup_locks.get(client_id)
    if lock is None:
        lock = asyncio.Lock()
        _client_setup_locks[client_id] = lock
    return lock


async def create_client_state(client_id: str, user, device_name: str | None = None):
    manager = get_client_manager()
    async with _client_setup_lock(client_id):
        if manager.has_client(client_id):
            await cleanup_client_state(client_id)
        state = manager.create_client(client_id, user.user_id, user.email)
    await track_client_user_relationship_async(client_id, user.user_id)
    await register_client_to_user(user, client_id, device_name)
    return state


async def cleanup_client_state(
    client_id: str, expected_connection_id: str | None = None
) -> bool:
    manager = get_client_manager()
    state = manager.get_client(client_id)
    if expected_connection_id is not None and (
        state is None or state.socket_id != expected_connection_id
    ):
        return False
    session_id = state.stream_session_id if state else None
    redis_client = create_async_redis(decode_responses=False)
    try:
        if session_id:
            store = SessionStore(redis_client)
            view = await store.read(session_id)
            if view is None or view.client_id != client_id:
                raise RuntimeError("connected client has an invalid session binding")
            if view.status is SessionStatus.ACTIVE:
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
            if view.stream_name and await redis_client.exists(view.stream_name):
                await redis_client.persist(view.stream_name)
                await inspect_stream_retention(
                    redis_client,
                    view.stream_name,
                    required_groups={AUDIO_PERSISTENCE_GROUP},
                )
            results_key = f"transcription:results:{session_id}"
            if await redis_client.exists(results_key):
                await redis_client.expire(results_key, 300)
            await redis_client.expire(f"speech_detection_job:{session_id}", 3600)
    finally:
        await redis_client.aclose()
    removed = (
        await manager.remove_client_with_cleanup(client_id)
        if expected_connection_id is None
        else await manager.remove_client_lease(client_id, expected_connection_id)
    )
    try:
        await touch_client_last_seen(client_id)
    except Exception:
        logger.debug("Could not stamp last_seen for %s", client_id, exc_info=True)
    return removed


async def initialize_capture_session(
    *,
    client_state,
    producer,
    user_id: str,
    user_email: str,
    client_id: str,
    source_format: dict,
    provenance: CaptureStartProvenance,
) -> None:
    if client_state.stream_session_id is not None:
        raise ValueError("capture is already active")
    registry = get_models_registry()
    stt_model = registry.get_default("stt") if registry else None
    if provenance.data_purpose == "annotation":
        provider = "disabled"
    elif stt_model is None:
        raise ValueError("No default STT model configured")
    else:
        provider = (stt_model.model_provider or stt_model.name).lower()

    session_id = f"{client_id}-{uuid.uuid4().hex}"
    interactive = provenance.processing_profile in {
        "duplex_aec",
        "duplex_isolated",
        "half_duplex",
    }
    pending_voice = None
    voice_session_id = provenance.voice_session_id
    voice_sessions = VoiceSessionCoordinator(producer.redis_client)
    if interactive and voice_session_id is None:
        pending_voice = await voice_sessions.start(
            user_id=user_id,
            client_id=client_id,
            audio_session_id=session_id,
            capture_epoch=provenance.capture_epoch,
            socket_id=client_state.socket_id,
            advertised_protocol=2,
        )
        voice_session_id = pending_voice.session.voice_session_id
    try:
        await producer.init_session(
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
            user_email=user_email,
            connection_id=client_state.socket_id,
            mode="streaming",
            provider=provider,
            capture_epoch=provenance.capture_epoch,
            processing_profile=provenance.processing_profile,
            effects=provenance.effects,
            voice_session_id=voice_session_id,
            data_purpose=provenance.data_purpose,
            memory_space_id=provenance.memory_space_id,
        )
    except Exception:
        if pending_voice is not None:
            await voice_sessions.end(
                voice_session_id=voice_session_id,
                user_id=user_id,
                client_id=client_id,
                audio_session_id=session_id,
                capture_epoch=provenance.capture_epoch,
                socket_id=client_state.socket_id,
                reason="capture_initialization_failed",
            )
        raise
    client_state.stream_session_id = session_id
    client_state.voice_duplex_protocol = 2
    client_state.capture_epoch = provenance.capture_epoch
    client_state.processing_profile = provenance.processing_profile
    client_state.capture_effects = provenance.effects.model_dump(mode="json")
    client_state.voice_session_id = voice_session_id
    client_state.data_purpose = provenance.data_purpose
    await producer.store.set_audio_format(session_id, source_format)
    await publish_sse_event_async(
        user_id, "session.started", {"session_id": session_id, "client_id": client_id}
    )


async def finalize_capture_session(
    *, client_state, producer, user_id: str, client_id: str
) -> None:
    session_id = client_state.stream_session_id
    if session_id is None:
        return
    await producer.finalize_session(session_id, completion_reason="user_stopped")
    if client_state.markers:
        await producer.store.set_markers(session_id, client_state.markers)
        client_state.markers.clear()
    await producer.store.mark_complete(session_id, "user_stopped")
    await publish_sse_event_async(
        user_id,
        "session.ended",
        {"session_id": session_id, "client_id": client_id, "reason": "user_stopped"},
    )
    client_state.stream_session_id = None
    client_state.last_persistence_healthcheck = 0.0


async def handle_button_event(
    *, client_state, button_state: str, user_id: str, client_id: str
) -> None:
    timestamp = time.time()
    marker = {
        "type": "button_event",
        "state": button_state,
        "timestamp": timestamp,
        "audio_uuid": None,
        "session_id": client_state.stream_session_id,
        "client_id": client_id,
    }
    client_state.add_marker(marker)
    try:
        state = ButtonState(button_state)
    except ValueError:
        return
    event = BUTTON_STATE_TO_EVENT.get(state)
    router = get_plugin_router()
    if event and router:
        await router.dispatch_event(
            event=event.value,
            user_id=user_id,
            data={**marker, "state": state.value},
        )
