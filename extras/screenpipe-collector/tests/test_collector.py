import sqlite3
from pathlib import Path

from chronicle_screenpipe.collector import (
    Checkpoints,
    Collector,
    Config,
    audio_duration,
    infer_audio_direction,
)


def test_checkpoints_are_atomic(tmp_path: Path):
    checkpoints = Checkpoints(tmp_path / "state.json")
    checkpoints.set("audio", 42)
    assert Checkpoints(tmp_path / "state.json").get("audio") == 42


def test_audio_direction_from_screenpipe_filename():
    assert infer_audio_direction("device (input)_2026.wav") == "input"
    assert infer_audio_direction("device (output)_2026.wav") == "output"


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
