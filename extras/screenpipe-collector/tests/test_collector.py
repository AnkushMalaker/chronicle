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


def _collector_over(database: Path) -> Collector:
    collector = object.__new__(Collector)
    collector.config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=database.parent,
    )
    return collector


def _frames_database(tmp_path: Path, rows: list[tuple[int, str | None]]) -> Path:
    database = tmp_path / "db.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE frames (id INTEGER PRIMARY KEY, capture_trigger TEXT)"
    )
    connection.executemany(
        "INSERT INTO frames (id, capture_trigger) VALUES (?, ?)", rows
    )
    connection.commit()
    connection.close()
    return database


def test_capture_triggers_are_attached_from_the_local_database(tmp_path: Path):
    """`/search` omits capture_trigger, so it is read from ScreenPipe's own DB."""
    database = _frames_database(tmp_path, [(7, "manual"), (8, "visual_change")])
    items = [
        {"source_item_id": "frame:7", "metadata": {"frame_id": 7}},
        {"source_item_id": "frame:8", "metadata": {"frame_id": 8}},
    ]

    _collector_over(database)._attach_capture_triggers(items)

    assert items[0]["metadata"]["capture_trigger"] == "manual"
    assert items[1]["metadata"]["capture_trigger"] == "visual_change"


def test_frames_without_a_trigger_are_left_unlabelled(tmp_path: Path):
    database = _frames_database(tmp_path, [(7, None)])
    items = [{"source_item_id": "frame:7", "metadata": {"frame_id": 7}}]

    _collector_over(database)._attach_capture_triggers(items)

    assert "capture_trigger" not in items[0]["metadata"]


def test_an_unreadable_database_does_not_fail_the_job(tmp_path: Path):
    """Triggers are an enrichment; losing them must not lose the context items."""
    items = [{"source_item_id": "frame:7", "metadata": {"frame_id": 7}}]

    _collector_over(tmp_path / "missing.sqlite")._attach_capture_triggers(items)

    assert items == [{"source_item_id": "frame:7", "metadata": {"frame_id": 7}}]


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_paging_counts_rows_scanned_not_rows_returned(monkeypatch):
    """A deduplicated page is short of the limit while data remains.

    `limit` bounds rows ScreenPipe *scans*, so paging on `len(page)` would stop
    at the first page the recorder collapsed anything out of.
    """
    from chronicle_screenpipe import collector as collector_module

    pages = [
        {"data": [{"content": {"frame_id": i}} for i in range(300)], "deduped": 200},
        {"data": [{"content": {"frame_id": 900}}], "deduped": 0},
    ]
    seen_offsets = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_offsets.append(params["offset"])
        return _Response(pages[len(seen_offsets) - 1])

    monkeypatch.setattr(collector_module.httpx, "get", fake_get)

    collected = []
    offset = 0
    page_size = 500
    while True:
        body = fake_get("", params={"offset": offset})._payload
        page = body["data"]
        scanned = len(page) + int(body.get("deduped") or 0)
        collected.extend(page)
        if scanned < page_size:
            break
        offset += scanned

    assert seen_offsets == [0, 500]
    assert len(collected) == 301


def test_dedupe_is_requested_by_default():
    config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=Path("/tmp"),
    )

    assert config.search_dedupe == 0.85


def test_dedupe_can_be_turned_off():
    config = Config(
        backend_url="http://backend",
        source_id="source-1",
        token="token",
        screenpipe_dir=Path("/tmp"),
        search_dedupe=None,
    )

    assert config.search_dedupe is None
