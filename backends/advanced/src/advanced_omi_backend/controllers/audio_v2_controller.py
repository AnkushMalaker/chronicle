"""Typed Chronicle audio-v2 WebSocket ingress.

This controller owns only transport translation. Capture lifecycle, durability,
voice framing, and inference remain behind their existing application services while
those services are migrated to generated Redis messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from functools import partial

from fastapi import WebSocket, WebSocketDisconnect
from google.protobuf import timestamp_pb2

from advanced_omi_backend.audio_contract.v2 import audio_pb2
from advanced_omi_backend.audio_contract.v2.codec import (
    AudioProtocolV2Error,
    RawOpusDecoder,
    parse_client_control_json,
    parse_media_envelope,
    serialize_media_envelope,
    serialize_server_control_json,
)
from advanced_omi_backend.auth import websocket_auth
from advanced_omi_backend.client_manager import generate_client_id
from advanced_omi_backend.controllers.capture_lifecycle import (
    DECODER_EXECUTOR,
    cleanup_client_state,
    create_client_state,
    finalize_capture_session,
    handle_button_event,
    initialize_capture_session,
)
from advanced_omi_backend.controllers.queue_controller import start_streaming_jobs
from advanced_omi_backend.models.audio_capabilities import VoiceCapabilities
from advanced_omi_backend.models.audio_capture import (
    CaptureEffects,
    CaptureStartProvenance,
)
from advanced_omi_backend.redis_keys import ClientId, device_downlink_channel
from advanced_omi_backend.services.audio_stream.producer import (
    get_audio_stream_producer,
)
from advanced_omi_backend.services.audio_stream.v2_streams import AudioV2Streams
from advanced_omi_backend.services.memory.scope import (
    MemoryScope,
    MemoryScopeError,
    MemoryScopeResolver,
)
from advanced_omi_backend.services.response_coordinator import (
    ResponseCoordinator,
    ResponseCoordinatorError,
)
from advanced_omi_backend.services.voice_sessions import VoiceSessionCoordinator

AUDIO_SUBPROTOCOL = "chronicle.audio.v2"
logger = logging.getLogger(__name__)
_memory_scopes = MemoryScopeResolver()

_PROFILE_TO_DOMAIN = {
    audio_pb2.PROCESSING_PROFILE_AMBIENT: "ambient",
    audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE: "source_native",
    audio_pb2.PROCESSING_PROFILE_DUPLEX_AEC: "duplex_aec",
    audio_pb2.PROCESSING_PROFILE_DUPLEX_ISOLATED: "duplex_isolated",
    audio_pb2.PROCESSING_PROFILE_HALF_DUPLEX: "half_duplex",
    audio_pb2.PROCESSING_PROFILE_IMPORTED: "imported",
}
_PURPOSE_TO_DOMAIN = {
    audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE: "normal_capture",
    audio_pb2.DATA_PURPOSE_ANNOTATION: "annotation",
}
_STOP_REASON_TO_DOMAIN = {
    audio_pb2.STOP_REASON_USER_REQUESTED: "user_stopped",
    audio_pb2.STOP_REASON_AUDIO_DISCONNECT: "websocket_disconnect",
    audio_pb2.STOP_REASON_INTERACTION_COMPLETE: "user_stopped",
    audio_pb2.STOP_REASON_TEMPORARILY_UNAVAILABLE: "websocket_disconnect",
}


def _now() -> timestamp_pb2.Timestamp:
    value = timestamp_pb2.Timestamp()
    value.FromDatetime(datetime.now(timezone.utc))
    return value


def _event_id() -> audio_pb2.EventId:
    return audio_pb2.EventId(value=str(uuid.uuid4()))


async def _send_control(websocket: WebSocket, **event) -> None:
    message = audio_pb2.ServerControl(event_id=_event_id(), sent_at=_now(), **event)
    await websocket.send_text(serialize_server_control_json(message))


async def _subscribe_v2_downlink(
    *,
    websocket: WebSocket,
    redis_client,
    voice_sessions,
    responses,
    client_state,
    user_id: str,
    client_id: str,
) -> None:
    """Validate typed Redis downlink events and forward them to one bound socket."""

    channel = str(device_downlink_channel(ClientId.from_value(client_id)))
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if not message or message["type"] != "message":
                continue
            event = audio_pb2.DeviceDownlinkEvent()
            try:
                event.ParseFromString(message["data"])
            except Exception:
                continue
            kind = event.WhichOneof("event")
            if kind == "playback_offer":
                offer = event.playback_offer
                binding = offer.binding
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=binding.capture_session_id.value,
                    voice_session_id=binding.voice_session_id.value,
                    capture_epoch=binding.capture_epoch,
                    socket_id=client_state.socket_id,
                    require_ready=True,
                ):
                    continue
                await _send_control(websocket, playback_offer=offer)
            elif kind == "playback":
                packet = event.playback
                record = await responses.get(packet.response_id.value)
                if record is None or record.generation != packet.generation:
                    continue
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=record.audio_session_id,
                    voice_session_id=record.voice_session_id,
                    capture_epoch=record.capture_epoch,
                    socket_id=client_state.socket_id,
                    require_ready=True,
                ):
                    continue
                await websocket.send_bytes(
                    serialize_media_envelope(audio_pb2.MediaEnvelope(playback=packet))
                )
            elif kind == "cancel_playback":
                cancel = event.cancel_playback
                binding = cancel.binding
                if not await voice_sessions.binding_matches(
                    user_id=user_id,
                    client_id=client_id,
                    audio_session_id=binding.capture_session_id.value,
                    voice_session_id=binding.voice_session_id.value,
                    capture_epoch=binding.capture_epoch,
                    socket_id=client_state.socket_id,
                ):
                    continue
                await _send_control(websocket, cancel_playback=cancel)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


async def _subscribe_v2_transcripts(
    *,
    websocket: WebSocket,
    redis_client,
    binding: audio_pb2.CaptureBinding,
    subscribed: asyncio.Event | None = None,
) -> None:
    """Forward streaming STT pub/sub messages as typed, capture-bound controls."""

    channel = f"transcription:interim:{binding.capture_session_id.value}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    if subscribed is not None:
        subscribed.set()
    logger.info("Subscribed Audio V2 transcript channel: %s", channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if not message or message["type"] != "message":
                continue
            try:
                payload = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                logger.warning("Ignored malformed transcript update on %s", channel)
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            await _send_control(
                websocket,
                transcript_update=audio_pb2.TranscriptUpdate(
                    binding=binding,
                    text=text,
                    is_final=bool(payload.get("is_final", False)),
                    confidence=float(payload.get("confidence") or 0.0),
                    speaker_name=(
                        payload.get("speaker_name")
                        if isinstance(payload.get("speaker_name"), str)
                        else ""
                    ),
                ),
            )
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


async def _decode_opus(decoder: RawOpusDecoder, payload: bytes) -> bytes:
    return await asyncio.get_running_loop().run_in_executor(
        DECODER_EXECUTOR,
        partial(decoder.decode_packet, payload),
    )


def _effects(start: audio_pb2.StartCapture) -> CaptureEffects:
    interactive = start.processing_profile in {
        audio_pb2.PROCESSING_PROFILE_DUPLEX_AEC,
        audio_pb2.PROCESSING_PROFILE_DUPLEX_ISOLATED,
        audio_pb2.PROCESSING_PROFILE_HALF_DUPLEX,
    }
    if not interactive:
        return CaptureEffects.unreported()
    capabilities = start.capabilities
    return CaptureEffects.model_validate(
        {
            "aec": {
                "reporting": "reported",
                "requested": capabilities.acoustic_echo_cancellation.requested,
                "available": capabilities.acoustic_echo_cancellation.available,
                "enabled": capabilities.acoustic_echo_cancellation.enabled,
            },
            "noise_suppression": {
                "reporting": "reported",
                "requested": capabilities.noise_suppression.requested,
                "available": capabilities.noise_suppression.available,
                "enabled": capabilities.noise_suppression.enabled,
            },
        }
    )


def _start_provenance(start: audio_pb2.StartCapture) -> CaptureStartProvenance:
    profile = _PROFILE_TO_DOMAIN[start.processing_profile]
    if profile == "source_native" and start.capture_epoch != 0:
        raise AudioProtocolV2Error("source-native capture requires epoch zero")
    return CaptureStartProvenance(
        protocol=2,
        capture_epoch=start.capture_epoch,
        processing_profile=profile,
        effects=_effects(start),
        data_purpose=_PURPOSE_TO_DOMAIN[start.data_purpose],
        memory_space_id=start.memory_space_id.value or None,
    )


def _voice_capabilities(
    capabilities: audio_pb2.CaptureCapabilities,
) -> VoiceCapabilities:
    return VoiceCapabilities.model_validate(
        {
            "mode": {
                audio_pb2.DUPLEX_MODE_FULL: "duplex_full",
                audio_pb2.DUPLEX_MODE_ISOLATED: "duplex_isolated",
                audio_pb2.DUPLEX_MODE_HALF: "duplex_half",
            }[capabilities.duplex_mode],
            "input_route": {
                audio_pb2.INPUT_ROUTE_BUILT_IN_MIC: "built_in_mic",
                audio_pb2.INPUT_ROUTE_BLUETOOTH_HFP: "bluetooth_hfp",
                audio_pb2.INPUT_ROUTE_WIRED_MIC: "wired_mic",
                audio_pb2.INPUT_ROUTE_USB: "usb",
                audio_pb2.INPUT_ROUTE_REMOTE: "remote",
            }[capabilities.input_route],
            "output_route": {
                audio_pb2.OUTPUT_ROUTE_SPEAKERPHONE: "speakerphone",
                audio_pb2.OUTPUT_ROUTE_EARPIECE: "earpiece",
                audio_pb2.OUTPUT_ROUTE_HEADPHONES: "headphones",
                audio_pb2.OUTPUT_ROUTE_BLUETOOTH_HFP: "bluetooth_hfp",
                audio_pb2.OUTPUT_ROUTE_USB: "usb",
                audio_pb2.OUTPUT_ROUTE_REMOTE: "remote",
            }[capabilities.output_route],
            "native_sample_rate": capabilities.native_sample_rate_hz,
            "aec": {
                "requested": capabilities.acoustic_echo_cancellation.requested,
                "available": capabilities.acoustic_echo_cancellation.available,
                "enabled": capabilities.acoustic_echo_cancellation.enabled,
            },
            "noise_suppression": {
                "requested": capabilities.noise_suppression.requested,
                "available": capabilities.noise_suppression.available,
                "enabled": capabilities.noise_suppression.enabled,
            },
            "fallback_reason": (
                "aec_unavailable"
                if capabilities.duplex_mode == audio_pb2.DUPLEX_MODE_HALF
                else None
            ),
        }
    )


async def ingest_capture_packet(
    *,
    websocket: WebSocket,
    packet: audio_pb2.CaptureMediaPacket,
    client_state,
    audio_stream_producer,
    user,
    decoder: RawOpusDecoder,
    v2_streams: AudioV2Streams,
) -> None:
    """Decode one bound Opus packet and cross the durable/realtime seams."""

    session_id = client_state.stream_session_id
    if session_id is None:
        raise AudioProtocolV2Error("media arrived before capture start")
    if packet.binding.capture_session_id.value != session_id:
        raise AudioProtocolV2Error("capture packet has a stale session binding")
    if packet.binding.capture_epoch != client_state.capture_epoch:
        raise AudioProtocolV2Error("capture packet has a stale epoch binding")
    expected_voice = client_state.voice_session_id or ""
    if packet.binding.voice_session_id.value != expected_voice:
        raise AudioProtocolV2Error("capture packet has a stale voice binding")

    pcm = await _decode_opus(decoder, packet.opus_payload)
    expected_bytes = 16_000 * 1 * 2 * 20 // 1_000
    if not pcm or len(pcm) != expected_bytes:
        raise AudioProtocolV2Error(
            f"Opus packet decoded to {len(pcm) if pcm else 0} bytes; "
            f"expected {expected_bytes}"
        )

    await v2_streams.publish_frame(
        audio_pb2.CaptureStreamEvent(
            frame=audio_pb2.CanonicalPcmFrame(
                binding=packet.binding,
                sequence=packet.sequence,
                captured_at=packet.captured_at,
                monotonic_offset_us=packet.monotonic_offset_us,
                delivery_class=packet.delivery_class,
                pcm_s16le=pcm,
                data_purpose={
                    "normal_capture": audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
                    "annotation": audio_pb2.DATA_PURPOSE_ANNOTATION,
                }[client_state.data_purpose],
            )
        )
    )
    if packet.delivery_class == audio_pb2.DELIVERY_CLASS_RECOVERED:
        # Recovery is durable-only by construction. The typed persistence consumer
        # will promote this event to Mongo; it must never enter the old live stream.
        return


async def handle_audio_v2_websocket(websocket: WebSocket) -> None:
    offered = websocket.scope.get("subprotocols", [])
    if AUDIO_SUBPROTOCOL not in offered:
        await websocket.close(code=1002, reason="chronicle.audio.v2 required")
        return
    await websocket.accept(subprotocol=AUDIO_SUBPROTOCOL)

    client_state = None
    client_id = None
    producer = None
    interim_task = None
    downlink_task = None
    v2_streams = None
    try:
        first = await websocket.receive_text()
        hello_control = parse_client_control_json(first)
        if hello_control.WhichOneof("event") != "hello":
            raise AudioProtocolV2Error("first control must be ClientHello")
        hello = hello_control.hello
        user, _failure = await websocket_auth(websocket, hello.bearer_token)
        if user is None:
            await _send_control(
                websocket,
                error=audio_pb2.ProtocolError(
                    code=audio_pb2.PROTOCOL_ERROR_CODE_AUTHENTICATION_FAILED,
                    detail="authentication failed",
                    rejected_event_id=hello_control.event_id,
                ),
            )
            await websocket.close(code=1008, reason="authentication failed")
            return

        device_name = hello.display_name or hello.source_id.value
        client_id = generate_client_id(user, device_name)
        client_state = await create_client_state(client_id, user, device_name)
        client_state.socket_id = f"audio-v2-{uuid.uuid4()}"
        producer = get_audio_stream_producer()
        voice_sessions = VoiceSessionCoordinator(producer.redis_client)
        responses = ResponseCoordinator(producer.redis_client, voice_sessions)
        downlink_task = asyncio.create_task(
            _subscribe_v2_downlink(
                websocket=websocket,
                redis_client=producer.redis_client,
                voice_sessions=voice_sessions,
                responses=responses,
                client_state=client_state,
                user_id=user.user_id,
                client_id=client_id,
            )
        )
        await _send_control(
            websocket,
            hello=audio_pb2.ServerHello(
                client_id=audio_pb2.ClientId(value=client_id),
                connection_id=audio_pb2.ConnectionId(value=client_state.socket_id),
            ),
        )

        decoder = RawOpusDecoder()
        active_delivery_class = audio_pb2.DELIVERY_CLASS_UNSPECIFIED
        last_sequence = -1
        while True:
            incoming = await websocket.receive()
            if incoming.get("type") == "websocket.disconnect":
                break
            if incoming.get("text") is not None:
                control = parse_client_control_json(incoming["text"])
                event = control.WhichOneof("event")
                if event == "start_capture":
                    if client_state.stream_session_id is not None:
                        raise AudioProtocolV2Error("capture is already active")
                    start = control.start_capture
                    active_delivery_class = start.delivery_class
                    provenance = _start_provenance(start)
                    if provenance.memory_space_id:
                        try:
                            await _memory_scopes.require_space(
                                MemoryScope(
                                    str(user.user_id), provenance.memory_space_id
                                ),
                                writable=True,
                            )
                        except MemoryScopeError as exc:
                            raise AudioProtocolV2Error(str(exc)) from exc
                    await initialize_capture_session(
                        client_state=client_state,
                        producer=producer,
                        user_id=user.user_id,
                        user_email=user.email,
                        client_id=client_id,
                        source_format={
                            "codec": "opus",
                            "rate": 16_000,
                            "width": 2,
                            "channels": 1,
                            "frame_duration_ms": 20,
                            "mode": "streaming",
                        },
                        provenance=provenance,
                    )
                    binding = audio_pb2.CaptureBinding(
                        capture_session_id=audio_pb2.CaptureSessionId(
                            value=client_state.stream_session_id
                        ),
                        voice_session_id=audio_pb2.VoiceSessionId(
                            value=client_state.voice_session_id or ""
                        ),
                        capture_epoch=client_state.capture_epoch,
                    )
                    v2_streams = await AudioV2Streams.open(
                        producer.redis_client,
                        event=audio_pb2.CaptureStreamEvent(
                            opened=audio_pb2.CaptureStreamOpened(
                                binding=binding,
                                client_id=audio_pb2.ClientId(value=client_id),
                                source_id=hello.source_id,
                                source_spec=start.audio_spec,
                                processing_profile=start.processing_profile,
                                data_purpose=start.data_purpose,
                                memory_space_id=start.memory_space_id,
                            )
                        ),
                        delivery_class=start.delivery_class,
                    )
                    transcript_subscribed = asyncio.Event()
                    interim_task = asyncio.create_task(
                        _subscribe_v2_transcripts(
                            websocket=websocket,
                            redis_client=producer.redis_client,
                            binding=binding,
                            subscribed=transcript_subscribed,
                        )
                    )
                    await transcript_subscribed.wait()
                    job_ids = await asyncio.to_thread(
                        start_streaming_jobs,
                        session_id=client_state.stream_session_id,
                        user_id=user.user_id,
                        client_id=client_id,
                        speech_detection_enabled=(
                            start.data_purpose != audio_pb2.DATA_PURPOSE_ANNOTATION
                        ),
                        contract_version=2,
                    )
                    await producer.update_session_job_ids(
                        session_id=client_state.stream_session_id,
                        speech_detection_job_id=job_ids["speech_detection"],
                        audio_persistence_job_id=job_ids["audio_persistence"],
                    )
                    await _send_control(
                        websocket,
                        capture_started=audio_pb2.CaptureStarted(
                            binding=binding, audio_spec=start.audio_spec
                        ),
                    )
                elif event == "stop_capture":
                    if control.stop_capture.binding.capture_session_id.value != (
                        client_state.stream_session_id or ""
                    ):
                        raise AudioProtocolV2Error("stop has a stale capture binding")
                    binding = control.stop_capture.binding
                    await finalize_capture_session(
                        client_state=client_state,
                        producer=producer,
                        user_id=user.user_id,
                        client_id=client_id,
                    )
                    if v2_streams is None:
                        raise AudioProtocolV2Error("capture stream was not opened")
                    await v2_streams.end(
                        audio_pb2.CaptureStreamEvent(
                            ended=audio_pb2.CaptureStreamEnded(
                                binding=binding,
                                reason=control.stop_capture.reason,
                            )
                        )
                    )
                    await _send_control(
                        websocket,
                        capture_stopped=audio_pb2.CaptureStopped(binding=binding),
                    )
                    if interim_task is not None:
                        interim_task.cancel()
                        await asyncio.gather(interim_task, return_exceptions=True)
                        interim_task = None
                    active_delivery_class = audio_pb2.DELIVERY_CLASS_UNSPECIFIED
                    last_sequence = -1
                    v2_streams = None
                elif event == "heartbeat":
                    await _send_control(websocket, heartbeat=control.heartbeat)
                elif event == "voice_ready":
                    ready = control.voice_ready
                    if (
                        ready.binding.capture_session_id.value
                        != (client_state.stream_session_id or "")
                        or ready.binding.voice_session_id.value
                        != (client_state.voice_session_id or "")
                        or ready.binding.capture_epoch != client_state.capture_epoch
                    ):
                        raise AudioProtocolV2Error("voice-ready has a stale binding")
                    await voice_sessions.ready(
                        voice_session_id=client_state.voice_session_id,
                        user_id=user.user_id,
                        client_id=client_id,
                        audio_session_id=client_state.stream_session_id,
                        capture_epoch=client_state.capture_epoch,
                        socket_id=client_state.socket_id,
                        capabilities=_voice_capabilities(ready.capabilities),
                    )
                elif event == "playback_acknowledgement":
                    acknowledgement = control.playback_acknowledgement
                    binding = acknowledgement.binding
                    state = {
                        audio_pb2.PLAYBACK_STATE_STARTED: "started",
                        audio_pb2.PLAYBACK_STATE_DONE: "done",
                        audio_pb2.PLAYBACK_STATE_CANCELLED: "cancelled",
                        audio_pb2.PLAYBACK_STATE_FAILED: "failed",
                    }.get(acknowledgement.state)
                    if state is None:
                        raise AudioProtocolV2Error("unsupported playback state")
                    try:
                        await responses.playback(
                            response_id=acknowledgement.response_id.value,
                            generation=acknowledgement.generation,
                            state=state,
                            user_id=user.user_id,
                            client_id=client_id,
                            audio_session_id=binding.capture_session_id.value,
                            voice_session_id=binding.voice_session_id.value,
                            capture_epoch=binding.capture_epoch,
                            socket_id=client_state.socket_id,
                            monotonic_timestamp_ms=(
                                acknowledgement.monotonic_timestamp_us // 1_000
                            ),
                        )
                    except ResponseCoordinatorError as error:
                        raise AudioProtocolV2Error(str(error)) from error
                elif event == "button_event":
                    button_state = {
                        audio_pb2.BUTTON_STATE_SINGLE_PRESS: "SINGLE_PRESS",
                        audio_pb2.BUTTON_STATE_DOUBLE_PRESS: "DOUBLE_PRESS",
                        audio_pb2.BUTTON_STATE_LONG_PRESS: "LONG_PRESS",
                    }.get(control.button_event.state)
                    if button_state is None:
                        raise AudioProtocolV2Error("button event requires a state")
                    await handle_button_event(
                        client_state=client_state,
                        button_state=button_state,
                        user_id=user.user_id,
                        client_id=client_id,
                    )
                else:
                    raise AudioProtocolV2Error(
                        f"unsupported client control during capture: {event}"
                    )
            elif incoming.get("bytes") is not None:
                envelope = parse_media_envelope(incoming["bytes"])
                if envelope.WhichOneof("media") != "capture":
                    raise AudioProtocolV2Error("client may only send capture media")
                packet = envelope.capture
                if packet.delivery_class != active_delivery_class:
                    raise AudioProtocolV2Error(
                        "packet delivery class differs from capture start"
                    )
                if packet.sequence <= last_sequence:
                    raise AudioProtocolV2Error("packet sequence is not increasing")
                if v2_streams is None:
                    raise AudioProtocolV2Error("media arrived before stream open")
                await ingest_capture_packet(
                    websocket=websocket,
                    packet=packet,
                    client_state=client_state,
                    audio_stream_producer=producer,
                    user=user,
                    decoder=decoder,
                    v2_streams=v2_streams,
                )
                await _send_control(
                    websocket,
                    capture_packet_accepted=audio_pb2.CapturePacketAccepted(
                        binding=packet.binding,
                        sequence=packet.sequence,
                    ),
                )
                last_sequence = packet.sequence
            else:
                raise AudioProtocolV2Error("unsupported WebSocket message")
    except WebSocketDisconnect:
        pass
    except AudioProtocolV2Error as error:
        logger.warning(
            "Rejecting audio-v2 client=%s session=%s: %s",
            client_id,
            getattr(client_state, "stream_session_id", None),
            error,
        )
        try:
            await _send_control(
                websocket,
                error=audio_pb2.ProtocolError(
                    code=audio_pb2.PROTOCOL_ERROR_CODE_INVALID_MEDIA,
                    detail=str(error),
                ),
            )
            await websocket.close(code=1008, reason="invalid audio-v2 message")
        except Exception:
            pass
    finally:
        if interim_task is not None and not interim_task.done():
            interim_task.cancel()
        if downlink_task is not None and not downlink_task.done():
            downlink_task.cancel()
            try:
                await downlink_task
            except asyncio.CancelledError:
                pass
        if client_id is not None and client_state is not None:
            await cleanup_client_state(client_id, client_state.socket_id)
