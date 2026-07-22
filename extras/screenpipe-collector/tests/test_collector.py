import sqlite3
from pathlib import Path

from chronicle_screenpipe.collector import Checkpoints, audio_duration, build_activity_sessions, fold_activity_rows, infer_audio_direction


def test_activity_sessions_collapse_same_window():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE frames (id INTEGER, timestamp TEXT, app_name TEXT, window_name TEXT, browser_url TEXT, capture_trigger TEXT)")
    db.executemany("INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "2026-07-22T10:00:00", "Code", "chronicle", None, "AppSwitch"),
        (2, "2026-07-22T10:00:05", "Code", "chronicle", None, "Keystroke"),
        (3, "2026-07-22T10:01:00", "Game", "Game", None, "AppSwitch"),
    ])
    sessions = build_activity_sessions(db.execute("SELECT * FROM frames ORDER BY id"))
    assert [(s["app_name"], s["frame_count"]) for s in sessions] == [("Code", 2), ("Game", 1)]


def test_checkpoints_are_atomic(tmp_path: Path):
    checkpoints = Checkpoints(tmp_path / "state.json")
    checkpoints.set("audio", 42)
    assert Checkpoints(tmp_path / "state.json").get("audio") == 42


def test_audio_direction_from_screenpipe_filename():
    assert infer_audio_direction("device (input)_2026.wav") == "input"
    assert infer_audio_direction("device (output)_2026.wav") == "output"


def test_activity_session_survives_poll_boundary():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE frames (id INTEGER, timestamp TEXT, app_name TEXT, window_name TEXT, browser_url TEXT, capture_trigger TEXT)")
    db.executemany("INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "2026-07-22T10:00:00", "Game", "Game", None, "AppSwitch"),
        (2, "2026-07-22T10:00:05", "Game", "Game", None, "VisualChange"),
        (3, "2026-07-22T10:01:00", "Code", "chronicle", None, "AppSwitch"),
    ])
    closed, current = fold_activity_rows(db.execute("SELECT * FROM frames WHERE id <= 1"), None)
    assert closed == []
    closed, current = fold_activity_rows(db.execute("SELECT * FROM frames WHERE id > 1"), current)
    assert closed[0]["source_item_id"] == "activity:1"
    assert closed[0]["frame_count"] == 2
    assert current["app_name"] == "Code"


def test_wav_duration_is_read_from_media(tmp_path: Path):
    import wave
    target = tmp_path / "sample.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 8000)
    assert audio_duration(target) == 0.5
