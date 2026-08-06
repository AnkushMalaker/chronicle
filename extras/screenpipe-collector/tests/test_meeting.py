import json
import subprocess

from chronicle_screenpipe import meeting as meeting_module
from chronicle_screenpipe.meeting import (
    CaptureApp,
    MeetingTracker,
    classify_capture,
    pipewire_capture_apps,
)


def at(seconds: int) -> str:
    return f"2026-07-22T10:{seconds // 60:02d}:{seconds % 60:02d}+00:00"


ZOOM = [CaptureApp(name="Zoom Workplace", binary="zoom")]
FIREFOX = [CaptureApp(name="Firefox", binary="firefox")]
MEET_CONTEXT = {
    "app_name": "Firefox",
    "window_name": "Weekly sync – Meet",
    "browser_url": "https://meet.google.com/abc-defg-hij",
}


# --- classification -----------------------------------------------------------


def test_native_meeting_app_classifies_directly():
    detection = classify_capture(ZOOM, None)
    assert detection == {"platform": "zoom", "app": "zoom"}


def test_browser_mic_is_attributed_through_the_observed_url():
    detection = classify_capture(FIREFOX, MEET_CONTEXT)
    assert detection["platform"] == "google-meet"
    assert detection["browser_url"] == "https://meet.google.com/abc-defg-hij"


def test_browser_mic_without_a_meeting_url_is_still_a_call():
    detection = classify_capture(FIREFOX, {"browser_url": ""})
    assert detection == {"platform": "browser-call", "app": "firefox"}


def test_always_on_capturers_never_classify():
    """ScreenPipe's own capture streams and other non-call apps are ignored."""
    apps = [
        CaptureApp(name="screenpipe", binary="screenpipe"),
        CaptureApp(name="OBS Studio", binary="obs"),
        CaptureApp(name="Handy", binary="handy"),
    ]
    assert classify_capture(apps, MEET_CONTEXT) is None


# --- state machine ------------------------------------------------------------


def test_one_sighting_does_not_open_a_meeting():
    tracker = MeetingTracker()
    assert tracker.tick(ZOOM, None, at(0)) == []
    assert tracker.state["phase"] == "confirming"
    assert tracker.tick([], None, at(5)) == []
    assert tracker.state["phase"] == "idle"


def test_confirmed_sighting_opens_a_meeting_observation():
    tracker = MeetingTracker()
    tracker.tick(ZOOM, None, at(0))
    events = tracker.tick(ZOOM, None, at(5))
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "open"
    assert event["captured_at"] == at(0)
    assert event["metadata"]["observation_type"] == "meeting"
    assert event["metadata"]["platform"] == "zoom"
    assert event["frame_candidates"] == []
    assert event["sample"] is None
    assert tracker.active_platform() == "zoom"


def test_brief_capture_drop_does_not_end_the_meeting():
    tracker = MeetingTracker(grace_seconds=30)
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    assert tracker.tick([], None, at(10)) == []
    assert tracker.state["phase"] == "ending"
    assert tracker.tick(ZOOM, None, at(20)) == []
    assert tracker.state["phase"] == "active"


def test_capture_gone_past_grace_closes_at_the_drop_time():
    tracker = MeetingTracker(grace_seconds=30)
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    tracker.tick([], None, at(60))
    events = tracker.tick([], None, at(95))
    assert [event["event"] for event in events] == ["close"]
    assert events[0]["ended_at"] == at(60)
    assert tracker.state["phase"] == "idle"
    assert tracker.state["recent"][-1]["started_at"] == at(0)


def test_sensor_failure_holds_state_until_stale():
    tracker = MeetingTracker(stale_seconds=600)
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    assert tracker.tick(None, None, at(60)) == []
    assert tracker.state["phase"] == "active"
    events = tracker.tick(None, None, at(700))
    assert [event["event"] for event in events] == ["close"]
    assert events[0]["ended_at"] == at(5)


def test_generic_browser_call_is_refined_once_the_tab_is_seen():
    tracker = MeetingTracker()
    tracker.tick(FIREFOX, {"browser_url": ""}, at(0))
    events = tracker.tick(FIREFOX, MEET_CONTEXT, at(5))
    assert events[0]["metadata"]["platform"] == "google-meet"


def test_state_survives_a_restart():
    tracker = MeetingTracker()
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    revived = MeetingTracker(json.loads(json.dumps(tracker.state)))
    assert revived.active_platform() == "zoom"
    assert revived.tick(ZOOM, None, at(10)) == []


# --- chunk tagging ------------------------------------------------------------


def test_chunks_overlapping_the_active_meeting_are_tagged():
    tracker = MeetingTracker()
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    meeting_id = tracker.meeting["meeting_id"]
    assert tracker.meeting_for(at(100), at(130)) == meeting_id
    assert tracker.meeting_for("2026-07-22T09:58:00+00:00", at(0)) == meeting_id
    assert (
        tracker.meeting_for("2026-07-22T09:00:00+00:00", "2026-07-22T09:00:30+00:00")
        is None
    )


def test_chunks_are_tagged_against_closed_intervals_after_the_fact():
    tracker = MeetingTracker(grace_seconds=30)
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    tracker.tick([], None, at(300))
    tracker.tick([], None, at(335))
    meeting_id = tracker.state["recent"][-1]["meeting_id"]
    assert tracker.meeting_for(at(100), at(130)) == meeting_id
    assert tracker.meeting_for(at(400), at(430)) is None


# --- recorder-written meetings ------------------------------------------------


def _recorder_row(row_id: int, start: str, end: str | None, app="Zoom", title=None):
    return {
        "id": row_id,
        "meeting_start": start,
        "meeting_end": end,
        "meeting_app": app,
        "title": title,
    }


def test_recorder_open_row_emits_an_open_event():
    from chronicle_screenpipe.meeting import RecorderMeetingLog

    log = RecorderMeetingLog()
    events = log.sync([_recorder_row(7, at(0), None, app="Google Meet")], at(10))
    assert [event["event"] for event in events] == ["open"]
    assert events[0]["source_item_id"] == "meeting:recorder:7"
    assert events[0]["metadata"]["platform"] == "google-meet"
    assert events[0]["metadata"]["detection_source"] == "recorder"
    assert log.active_platform() == "google-meet"


def test_recorder_close_is_emitted_once_when_the_row_gains_an_end():
    from chronicle_screenpipe.meeting import RecorderMeetingLog

    log = RecorderMeetingLog()
    log.sync([_recorder_row(7, at(0), None)], at(10))
    events = log.sync([_recorder_row(7, at(0), at(600), title="Weekly sync")], at(700))
    assert [event["event"] for event in events] == ["close"]
    assert events[0]["ended_at"] == at(600)
    assert events[0]["metadata"]["title"] == "Weekly sync"
    assert log.sync([_recorder_row(7, at(0), at(600))], at(800)) == []


def test_meeting_recorded_while_the_collector_was_down_backfills_both_events():
    from chronicle_screenpipe.meeting import RecorderMeetingLog

    log = RecorderMeetingLog()
    events = log.sync([_recorder_row(3, at(0), at(1200))], at(2000))
    assert [event["event"] for event in events] == ["open", "close"]
    assert log.meeting_for(at(300), at(330)) == "meeting:recorder:3"
    assert log.meeting_for(at(1500), at(1530)) is None


def test_recent_recorder_rows_suppress_the_pipewire_tracker():
    from chronicle_screenpipe.meeting import RecorderMeetingLog

    log = RecorderMeetingLog()
    assert log.owns_detection(at(0)) is False
    log.sync([_recorder_row(1, at(0), at(60))], at(100))
    assert log.owns_detection(at(200)) is True


def test_retired_tracker_closes_its_open_meeting():
    tracker = MeetingTracker()
    tracker.tick(ZOOM, None, at(0))
    tracker.tick(ZOOM, None, at(5))
    events = tracker.retire()
    assert [event["event"] for event in events] == ["close"]
    assert tracker.state["phase"] == "idle"
    # The interval is retained so already-tagged chunks stay consistent.
    assert tracker.meeting_for(at(2), at(4)) is not None


# --- sensor -------------------------------------------------------------------


def _fake_pw_dump(monkeypatch, graph):
    def fake_run(command, capture_output=None, timeout=None, check=None):
        assert command == ["pw-dump"]
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(graph).encode(), stderr=b""
        )

    monkeypatch.setattr(meeting_module.subprocess, "run", fake_run)


def test_pw_dump_running_capture_streams_are_reported(monkeypatch):
    _fake_pw_dump(
        monkeypatch,
        [
            {
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "application.name": "Firefox",
                        "application.process.binary": "firefox",
                    },
                },
            },
            {
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "suspended",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "application.process.binary": "screenpipe",
                    },
                },
            },
            {
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {"media.class": "Audio/Source"},
                },
            },
        ],
    )
    assert pipewire_capture_apps() == [CaptureApp(name="Firefox", binary="firefox")]


def test_missing_pw_dump_reports_no_sensor(monkeypatch):
    def fake_run(command, capture_output=None, timeout=None, check=None):
        raise FileNotFoundError("pw-dump")

    monkeypatch.setattr(meeting_module.subprocess, "run", fake_run)
    assert pipewire_capture_apps() is None
