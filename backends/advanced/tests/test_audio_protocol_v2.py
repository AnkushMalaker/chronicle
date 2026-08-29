from datetime import datetime, timezone

import pytest
from google.protobuf import duration_pb2, timestamp_pb2

from advanced_omi_backend.audio_contract.v2 import audio_pb2
from advanced_omi_backend.audio_contract.v2.codec import (
    AudioProtocolV2Error,
    parse_client_control_json,
    parse_media_envelope,
    serialize_client_control_json,
    serialize_media_envelope,
)

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
