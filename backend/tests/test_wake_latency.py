from datetime import datetime, timedelta, timezone

from backend.services.wakeword.interaction_ledger import WakeInteractionFact
from backend.services.wakeword.latency import build_wake_latency_report

TRACE_ID = "7ce4d46b-232f-47f9-8148-d595ed344cf2"
START = datetime(2026, 2, 2, 2, 40, tzinfo=timezone.utc)


def _fact(stage, ordinal, milliseconds, *, payload=None):
    return WakeInteractionFact(
        wake_trace_id=TRACE_ID,
        stage=stage,
        ordinal=ordinal,
        occurred_at=START + timedelta(milliseconds=milliseconds),
        user_id="user-1",
        client_id="device-1",
        audio_session_id="audio-1",
        capture_epoch=4,
        voice_session_id="voice-1",
        turn_id="turn-1",
        payload=payload or {},
    )


def test_complete_trace_reports_each_owned_latency_segment_exactly():
    facts = [
        _fact("armed", 0, 0),
        _fact("end_of_turn", 1, 1_500),
        _fact("command_resolved", 2, 2_100),
        _fact(
            "dispatched",
            3,
            6_350,
            payload={
                "dispatch_ms": 4_250.0,
                "plugins": [{"plugin_id": "hermes", "duration_ms": 4_200.0}],
            },
        ),
        _fact("response_queued", 5, 6_400),
        _fact("response_ready", 6, 7_200),
        _fact("response_offered", 7, 7_225),
        _fact("response_playing", 8, 7_525),
        _fact("response_done", 9, 9_525),
    ]

    report = build_wake_latency_report(facts)

    assert report.status == "complete"
    assert report.missing_stages == ()
    assert report.metrics_ms == {
        "wake_capture": 1500.0,
        "turn_commit": 600.0,
        "plugin_dispatch": 4250.0,
        "response_queue": 50.0,
        "tts": 800.0,
        "offer_enqueue": 25.0,
        "device_start": 300.0,
        "playback": 2000.0,
        "end_of_turn_to_playing": 6025.0,
        "arm_to_playing": 7525.0,
        "arm_to_done": 9525.0,
    }
    assert report.plugins == ({"plugin_id": "hermes", "duration_ms": 4200.0},)


def test_incomplete_trace_names_missing_interfaces_without_inventing_latency():
    report = build_wake_latency_report(
        [_fact("armed", 0, 0), _fact("end_of_turn", 1, 1_500)]
    )

    assert report.status == "incomplete"
    assert report.metrics_ms == {"wake_capture": 1500.0}
    assert report.missing_stages == (
        "command_resolved",
        "dispatched",
        "response_queued",
        "response_ready",
        "response_offered",
        "response_playing",
        "response_done",
    )
