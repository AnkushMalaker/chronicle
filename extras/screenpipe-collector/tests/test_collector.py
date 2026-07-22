import sqlite3
from pathlib import Path

from chronicle_screenpipe.collector import (
    Checkpoints,
    Collector,
    Config,
    activity_is_salient,
    audio_duration,
    build_activity_sessions,
    fold_activity_rows,
    infer_audio_direction,
    text_excerpt,
)


def test_activity_sessions_collapse_same_window():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE frames (id INTEGER, timestamp TEXT, app_name TEXT, window_name TEXT, browser_url TEXT, capture_trigger TEXT)"
    )
    db.executemany(
        "INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-07-22T10:00:00", "Code", "chronicle", None, "AppSwitch"),
            (2, "2026-07-22T10:00:05", "Code", "chronicle", None, "Keystroke"),
            (3, "2026-07-22T10:01:00", "Game", "Game", None, "AppSwitch"),
        ],
    )
    sessions = build_activity_sessions(db.execute("SELECT * FROM frames ORDER BY id"))
    assert [(s["app_name"], s["frame_count"]) for s in sessions] == [
        ("Code", 2),
        ("Game", 1),
    ]


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
    db.execute(
        "CREATE TABLE frames (id INTEGER, timestamp TEXT, app_name TEXT, window_name TEXT, browser_url TEXT, capture_trigger TEXT)"
    )
    db.executemany(
        "INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-07-22T10:00:00", "Game", "Game", None, "AppSwitch"),
            (2, "2026-07-22T10:00:05", "Game", "Game", None, "VisualChange"),
            (3, "2026-07-22T10:01:00", "Code", "chronicle", None, "AppSwitch"),
        ],
    )
    closed, current = fold_activity_rows(
        db.execute("SELECT * FROM frames WHERE id <= 1"), None
    )
    assert closed == []
    closed, current = fold_activity_rows(
        db.execute("SELECT * FROM frames WHERE id > 1"), current
    )
    assert closed[0]["source_item_id"] == "activity:1"
    assert closed[0]["frame_count"] == 2
    assert current["app_name"] == "Code"


def test_activity_uses_bounded_text_and_its_frame_as_representative():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE frames (id INTEGER, timestamp TEXT, app_name TEXT, "
        "window_name TEXT, browser_url TEXT, capture_trigger TEXT, "
        "full_text TEXT, text_source TEXT)"
    )
    db.executemany(
        "INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-07-22T10:00:00", "Game", "Game", None, "AppSwitch", None, None),
            (
                2,
                "2026-07-22T10:00:05",
                "Game",
                "Game",
                None,
                "VisualChange",
                "  score\n  10  ",
                "accessibility",
            ),
        ],
    )
    session = build_activity_sessions(db.execute("SELECT * FROM frames"))[0]
    assert session["text"] == "score 10"
    assert session["text_source"] == "accessibility"
    assert session["representative_frame_id"] == 2
    assert len(text_excerpt("x" * 3000)) == 2000


def test_named_or_textual_changes_are_not_lost_to_time_debounce():
    base = {
        "captured_at": "2026-07-22T10:00:00Z",
        "ended_at": "2026-07-22T10:00:00Z",
        "frame_count": 1,
        "app_name": "",
        "window_name": "",
    }
    assert activity_is_salient({**base, "window_name": "A short-lived tab"})
    assert activity_is_salient({**base, "text": "terminal output"})
    assert not activity_is_salient(base)
    assert activity_is_salient(
        {
            **base,
            "ended_at": "2026-07-22T10:00:12Z",
            "frame_count": 2,
        }
    )


def test_wav_duration_is_read_from_media(tmp_path: Path):
    import wave

    target = tmp_path / "sample.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 8000)
    assert audio_duration(target) == 0.5


def test_collect_audio_accepts_screenpipe_startup_schema():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE audio_chunks (placeholder TEXT)")
    collector = object.__new__(Collector)
    assert collector.collect_audio(db) == 0


def test_collect_audio_checkpoints_sources_excluded_from_forwarding(tmp_path: Path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE audio_chunks (id INTEGER, file_path TEXT, timestamp TEXT)")
    db.execute(
        "INSERT INTO audio_chunks VALUES (1, ?, ?)",
        (str(tmp_path / "Microphone (input)_1.wav"), "2026-07-22T10:00:00Z"),
    )
    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://chronicle",
        source_id="rainbow",
        token="token",
        screenpipe_dir=tmp_path,
        forward_audio="output",
    )
    collector.checkpoints = Checkpoints(tmp_path / "state.json")

    assert collector.collect_audio(db) == 0
    assert collector.checkpoints.get("audio") == 1
