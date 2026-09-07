import asyncio
import json
from datetime import datetime, timezone

import pytest
from google.protobuf import duration_pb2, timestamp_pb2
from opuslib import Encoder

from backend.audio_contract.v2 import audio_pb2
from backend.audio_contract.v2.codec import (
    AudioProtocolV2Error,
    RawOpusNormalizer,
    parse_client_control_json,
    parse_media_envelope,
    serialize_client_control_json,
    serialize_media_envelope,
    validate_audio_spec,
)
from backend.controllers.audio_v2_controller import _subscribe_v2_transcripts

pytestmark = pytest.mark.unit


def _timestamp() -> timestamp_pb2.Timestamp:
    value = timestamp_pb2.Timestamp()
    value.FromDatetime(datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc))
    return value


def _spec() -> audio_pb2.AudioSpec:
    return audio_pb2.AudioSpec(
        codec=audio_pb2.AUDIO_CODEC_OPUS,
        sample_rate_hz=16_000,
        channel_count=1,
        frame_duration=duration_pb2.Duration(nanos=20_000_000),
        bitrate_bps=24_000,
    )


def test_raw_opus_decoder_accepts_three_byte_silence_packet():
    packet = Encoder(16_000, 1, "audio").encode(bytes(640), 320)
    assert len(packet) == 3

    assert RawOpusNormalizer(20).decode_frames(packet) == (bytes(640),)


def test_raw_opus_normalizer_splits_one_neo_packet_into_canonical_frames():
    packet = Encoder(16_000, 1, "audio").encode(bytes(1_920), 960)

    frames = RawOpusNormalizer(60).decode_frames(packet)

    assert len(frames) == 3
    assert all(len(frame) == 640 for frame in frames)


def test_uplink_spec_accepts_only_declared_20_or_60_ms_packets():
    validate_audio_spec(_spec(), live_uplink=True)
    sixty_ms = _spec()
    sixty_ms.frame_duration.nanos = 60_000_000
    validate_audio_spec(sixty_ms, live_uplink=True)

    forty_ms = _spec()
    forty_ms.frame_duration.nanos = 40_000_000
    with pytest.raises(AudioProtocolV2Error, match="20 or 60 ms"):
        validate_audio_spec(forty_ms, live_uplink=True)


def test_generated_control_json_round_trips_without_dictionary_contracts():
    message = audio_pb2.ClientControl(
        event_id=audio_pb2.EventId(value="event-1"),
        sent_at=_timestamp(),
        start_capture=audio_pb2.StartCapture(
            capture_epoch=4,
            processing_profile=audio_pb2.PROCESSING_PROFILE_DUPLEX_AEC,
            data_purpose=audio_pb2.DATA_PURPOSE_NORMAL_CAPTURE,
            delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
            audio_spec=_spec(),
        ),
    )

    rendered = serialize_client_control_json(message)
    parsed = parse_client_control_json(rendered)

    assert parsed == message
    assert parsed.WhichOneof("event") == "start_capture"


def test_control_json_rejects_unknown_fields_and_unspecified_enums():
    with pytest.raises(AudioProtocolV2Error, match="invalid ClientControl"):
        parse_client_control_json(
            '{"event_id":{"value":"e"},"sent_at":"2026-08-29T12:00:00Z",'
            '"heartbeat":{},"freeform":{"anything":true}}'
        )

    message = audio_pb2.ClientControl(
        event_id=audio_pb2.EventId(value="event-2"),
        sent_at=_timestamp(),
        start_capture=audio_pb2.StartCapture(audio_spec=_spec()),
    )
    with pytest.raises(AudioProtocolV2Error, match="processing_profile"):
        serialize_client_control_json(message)


def test_one_binary_frame_contains_binding_clock_and_raw_opus_payload():
    envelope = audio_pb2.MediaEnvelope(
        capture=audio_pb2.CaptureMediaPacket(
            binding=audio_pb2.CaptureBinding(
                capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
                voice_session_id=audio_pb2.VoiceSessionId(value="voice-1"),
                capture_epoch=7,
            ),
            sequence=11,
            captured_at=_timestamp(),
            monotonic_offset_us=220_000,
            delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
            opus_payload=b"\xf8\xff\xfe",
        )
    )

    encoded = serialize_media_envelope(envelope)
    parsed = parse_media_envelope(encoded)

    assert parsed == envelope
    assert parsed.capture.sequence == 11


def test_live_media_rejects_containerized_or_empty_audio():
    envelope = audio_pb2.MediaEnvelope(
        capture=audio_pb2.CaptureMediaPacket(
            binding=audio_pb2.CaptureBinding(
                capture_session_id=audio_pb2.CaptureSessionId(value="capture-1")
            ),
            captured_at=_timestamp(),
            delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
            opus_payload=b"OggS-not-raw-opus",
        )
    )
    with pytest.raises(AudioProtocolV2Error, match="containerized"):
        serialize_media_envelope(envelope)


def test_schema_has_no_untyped_maps_any_or_struct():
    file_descriptor = audio_pb2.DESCRIPTOR
    for message in file_descriptor.message_types_by_name.values():
        assert not message.GetOptions().map_entry
        for field in message.fields:
            assert field.message_type is None or field.message_type.full_name not in {
                "google.protobuf.Any",
                "google.protobuf.Struct",
            }


@pytest.mark.asyncio
async def test_transcript_pubsub_is_forwarded_as_typed_bound_server_control():
    class FakePubSub:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.subscribed = None

        async def subscribe(self, channel):
            self.subscribed = channel

        async def get_message(self, **_kwargs):
            return await self.messages.get()

        async def unsubscribe(self, _channel):
            pass

        async def close(self):
            pass

    class FakeRedis:
        def __init__(self, pubsub):
            self._pubsub = pubsub

        def pubsub(self):
            return self._pubsub

    class FakeWebSocket:
        def __init__(self):
            self.sent = asyncio.Queue()

        async def send_text(self, value):
            await self.sent.put(value)

    binding = audio_pb2.CaptureBinding(
        capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
        capture_epoch=0,
    )
    pubsub = FakePubSub()
    websocket = FakeWebSocket()
    subscribed = asyncio.Event()
    task = asyncio.create_task(
        _subscribe_v2_transcripts(
            websocket=websocket,
            redis_client=FakeRedis(pubsub),
            binding=binding,
            subscribed=subscribed,
        )
    )
    await asyncio.wait_for(subscribed.wait(), timeout=1)
    assert pubsub.subscribed == "transcription:interim:capture-1"

    await pubsub.messages.put(
        {
            "type": "message",
            "data": json.dumps(
                {
                    "text": "hello from the browser",
                    "is_final": True,
                    "confidence": 0.91,
                    "speaker_name": "Ankush",
                }
            ),
        }
    )
    raw = await asyncio.wait_for(websocket.sent.get(), timeout=1)
    control = audio_pb2.ServerControl()
    # Local import keeps this assertion's protobuf JSON helper scoped to the test.
    from google.protobuf import json_format

    json_format.Parse(raw, control)
    assert control.WhichOneof("event") == "transcript_update"
    assert control.transcript_update.binding == binding
    assert control.transcript_update.text == "hello from the browser"
    assert control.transcript_update.is_final is True
    assert control.transcript_update.confidence == pytest.approx(0.91)
    assert control.transcript_update.speaker_name == "Ankush"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
