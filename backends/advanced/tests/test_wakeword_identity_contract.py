import json

import pytest

from advanced_omi_backend.redis_keys import ClientId, SessionId, device_downlink_channel
from advanced_omi_backend.services.wakeword.contracts import WakeDetectionEvent


def test_device_downlink_channel_requires_client_identity():
    client_id = ClientId.from_value("a421c9-elato")

    assert str(device_downlink_channel(client_id)) == "device:downlink:a421c9-elato"

    with pytest.raises(TypeError, match="ClientId"):
        device_downlink_channel(SessionId.from_value("session-uuid"))


def test_wake_detection_event_requires_explicit_client_id():
    payload = {
        "session_id": "session-uuid",
        "user_id": "user-1",
        "audio_b64": "",
        "sample_rate": 16000,
    }

    with pytest.raises(ValueError, match="client_id"):
        WakeDetectionEvent.from_payload(payload)


def test_wake_detection_event_keeps_session_and_client_identity_distinct():
    payload = {
        "session_id": "session-uuid",
        "client_id": "a421c9-elato",
        "user_id": "user-1",
        "audio_b64": "",
        "sample_rate": 16000,
        "wake_trace_id": "7ce4d46b-232f-47f9-8148-d595ed344cf2",
        "capture_epoch": 7,
        "armed_at": 123.5,
        "end_of_turn_at": 124.5,
        "trigger_interval": {
            "start_ms": 500.0,
            "end_ms": 1500.0,
            "started_at": 122.5,
            "ended_at": 123.5,
        },
        "command_interval": {
            "start_ms": 1500.0,
            "end_ms": 2500.0,
            "started_at": 123.5,
            "ended_at": 124.5,
        },
        "has_speech": False,
        "wakeword": "hey_hermes",
        "also_fired": ["hermes"],
        "score": 0.91,
        "reason": "test",
    }

    event = WakeDetectionEvent.from_payload(json.loads(json.dumps(payload)))

    assert str(event.client_id) == "a421c9-elato"
    assert str(event.session_id) == "session-uuid"
    assert str(event.downlink_channel) == "device:downlink:a421c9-elato"
    assert event.wake_trace_id == "7ce4d46b-232f-47f9-8148-d595ed344cf2"
    assert event.command_interval.end_ms == 2500.0


def test_wake_detection_event_rejects_arrival_clock_without_capture_clock():
    payload = {
        "session_id": "session-uuid",
        "client_id": "a421c9-elato",
        "user_id": "user-1",
        "detected_at": 123.5,
    }

    with pytest.raises(ValueError, match="wake_trace_id"):
        WakeDetectionEvent.from_payload(payload)
