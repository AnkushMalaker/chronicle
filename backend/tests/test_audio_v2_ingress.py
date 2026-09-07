from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.protobuf import duration_pb2, timestamp_pb2

from backend.audio_contract.v2 import audio_pb2
from backend.audio_contract.v2.codec import AudioProtocolV2Error
from backend.controllers import audio_v2_controller

pytestmark = pytest.mark.unit


class Decoder:
    def decode_packet(self, payload):
        assert payload == b"raw-opus"
        return b"\x00\x00" * 320


def _packet(delivery_class=audio_pb2.DELIVERY_CLASS_LIVE):
    captured_at = timestamp_pb2.Timestamp()
    captured_at.FromDatetime(datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc))
    return audio_pb2.CaptureMediaPacket(
        binding=audio_pb2.CaptureBinding(
            capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
            voice_session_id=audio_pb2.VoiceSessionId(value="voice-1"),
            capture_epoch=9,
        ),
        sequence=12,
        captured_at=captured_at,
        monotonic_offset_us=240_000,
        delivery_class=delivery_class,
        opus_payload=b"raw-opus",
    )


def test_v2_start_preserves_noninteractive_source_native_provenance():
    start = audio_pb2.StartCapture(
        capture_epoch=0,
        processing_profile=audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose=audio_pb2.DATA_PURPOSE_ANNOTATION,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        audio_spec=audio_pb2.AudioSpec(
            codec=audio_pb2.AUDIO_CODEC_OPUS,
            sample_rate_hz=16_000,
            channel_count=1,
            frame_duration=duration_pb2.Duration(nanos=20_000_000),
        ),
    )

    provenance = audio_v2_controller._start_provenance(start)

    assert provenance.protocol == 2
    assert provenance.capture_epoch == 0
    assert provenance.processing_profile == "source_native"
    assert provenance.data_purpose == "annotation"
    assert provenance.effects.aec.reporting == "unreported"
    assert provenance.memory_space_id is None


def test_v2_start_preserves_typed_memory_space_id():
    start = audio_pb2.StartCapture(
        capture_epoch=0,
        processing_profile=audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        memory_space_id=audio_pb2.MemorySpaceId(
            value="9f3523c8-af75-469d-995a-7179531f3fc8"
        ),
    )

    provenance = audio_v2_controller._start_provenance(start)

    assert provenance.memory_space_id == "9f3523c8-af75-469d-995a-7179531f3fc8"


def test_v2_rejects_nonzero_source_native_epoch_at_protocol_boundary():
    start = audio_pb2.StartCapture(
        capture_epoch=1,
        processing_profile=audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE,
        data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
        audio_spec=audio_pb2.AudioSpec(
            codec=audio_pb2.AUDIO_CODEC_OPUS,
            sample_rate_hz=16_000,
            channel_count=1,
            frame_duration=duration_pb2.Duration(nanos=20_000_000),
        ),
    )

    with pytest.raises(AudioProtocolV2Error, match="source-native.*epoch zero"):
        audio_v2_controller._start_provenance(start)


async def test_v2_opus_decodes_once_then_crosses_realtime_and_durable_seams(
    monkeypatch,
):
    monkeypatch.setattr(
        audio_v2_controller,
        "_decode_opus",
        AsyncMock(return_value=b"\x00\x00" * 320),
    )
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id="voice-1",
        capture_epoch=9,
        data_purpose="normal_capture",
        client_id="client-1",
    )
    producer = SimpleNamespace(redis_client=object())
    user = SimpleNamespace(user_id="user-1", email="user@example.com")
    streams = SimpleNamespace(publish_frame=AsyncMock())

    await audio_v2_controller.ingest_capture_packet(
        websocket=object(),
        packet=_packet(),
        client_state=state,
        audio_stream_producer=producer,
        user=user,
        decoder=Decoder(),
        v2_streams=streams,
    )

    assert streams.publish_frame.await_args.args[0].WhichOneof("event") == "frame"
    assert streams.publish_frame.await_args.args[0].frame.pcm_s16le == b"\x00\x00" * 320


async def test_recovered_packet_enters_only_typed_durable_stream(monkeypatch):
    monkeypatch.setattr(
        audio_v2_controller,
        "_decode_opus",
        AsyncMock(return_value=b"\x00\x00" * 320),
    )
    state = SimpleNamespace(
        stream_session_id="capture-1",
        voice_session_id="voice-1",
        capture_epoch=9,
        data_purpose="normal_capture",
        client_id="client-1",
    )

    streams = SimpleNamespace(publish_frame=AsyncMock())
    await audio_v2_controller.ingest_capture_packet(
        websocket=object(),
        packet=_packet(audio_pb2.DELIVERY_CLASS_RECOVERED),
        client_state=state,
        audio_stream_producer=SimpleNamespace(redis_client=object()),
        user=SimpleNamespace(user_id="user-1", email="user@example.com"),
        decoder=Decoder(),
        v2_streams=streams,
    )

    streams.publish_frame.assert_awaited_once()


async def test_v2_media_rejects_stale_connection_binding():
    packet = _packet()
    packet.binding.capture_session_id.value = "old-capture"

    with pytest.raises(AudioProtocolV2Error, match="stale session"):
        await audio_v2_controller.ingest_capture_packet(
            websocket=object(),
            packet=packet,
            client_state=SimpleNamespace(
                stream_session_id="capture-1",
                voice_session_id="voice-1",
                capture_epoch=9,
                data_purpose="normal_capture",
                client_id="client-1",
            ),
            audio_stream_producer=SimpleNamespace(redis_client=object()),
            user=SimpleNamespace(user_id="user-1", email="user@example.com"),
            decoder=Decoder(),
            v2_streams=SimpleNamespace(publish_frame=AsyncMock()),
        )
