"""Chunks carry their own absolute capture time.

``captured_at`` is what turns a conversation from a container into a claim over an
interval. Before it, a chunk's time was defined by its parent, so trimmed or moved
audio lost all provenance. Both producers must set it, and no reassignment may
rewrite it.
"""

from datetime import datetime, timezone

from backend.workers.audio_jobs import _captured_at


def test_a_redis_stream_id_is_already_a_capture_timestamp():
    """The streaming path needs no new plumbing — Redis stamps the wall clock."""
    moment = datetime(2026, 8, 7, 18, 17, 59, tzinfo=timezone.utc)
    stream_id = f"{int(moment.timestamp() * 1000)}-0"

    assert _captured_at(stream_id) == moment


def test_the_sequence_suffix_does_not_disturb_the_time():
    first = _captured_at("1785089879000-0")
    later = _captured_at("1785089879000-41")
    assert first == later


def test_a_malformed_id_anchors_nothing_rather_than_lying():
    """An unanchored chunk is recoverable; a wrongly anchored one is not."""
    assert _captured_at("not-an-id") is None
    assert _captured_at("") is None
