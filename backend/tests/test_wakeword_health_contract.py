"""Deployment contract for the Audio V2 wake-word consumer."""

from backend.routers.modules.health_routes import evaluate_wakeword_health


def test_health_rejects_running_legacy_wake_consumer():
    result = evaluate_wakeword_health(
        {
            "status": "ok",
            "consumer_alive": True,
            "consumer_group": "wakeword_detection",
            "stream_pattern": "audio:stream:*",
        },
        "http://wakeword:8770",
    )

    assert result["healthy"] is False
    assert result["status"] == "Audio V2 contract mismatch"
    assert result["expected_consumer_group"] == "wakeword-v2"
    assert result["expected_stream_pattern"] == "audio:v2:realtime:*"


def test_health_accepts_live_audio_v2_wake_consumer():
    result = evaluate_wakeword_health(
        {
            "status": "ok",
            "consumer_alive": True,
            "consumer_group": "wakeword-v2",
            "stream_pattern": "audio:v2:realtime:*",
        },
        "http://wakeword:8770",
    )

    assert result["healthy"] is True
    assert result["status"] == "Connected"
