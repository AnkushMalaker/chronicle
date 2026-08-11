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
        "detected_at": 123.5,
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
