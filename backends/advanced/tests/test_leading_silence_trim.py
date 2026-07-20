"""Computing where to trim leading silence off a conversation.

With always_persist on, a session's placeholder accumulates audio from session
start — so a long pause before the user speaks ends up as leading silence on the
conversation. At finalize we split that leading silence off into a soft-deleted
remnant so the visible conversation begins at the first speech (the audio is kept
in Mongo on the remnant, just hidden).

``leading_silence_trim_index`` is the pure decision at the heart of that: given the
chunk timeline and when speech first starts, which chunk_index should become the
first chunk of the trimmed conversation — or None when there isn't enough leading
silence to bother (so we never churn conversations over a few seconds of pre-roll).
"""

import pytest

from advanced_omi_backend.workers.conversation_jobs import leading_silence_trim_index

pytestmark = pytest.mark.unit


def _ten_second_chunks(count):
    return [
        {"chunk_index": i, "start_time": i * 10.0, "end_time": (i + 1) * 10.0}
        for i in range(count)
    ]


def test_long_leading_silence_returns_speech_boundary_chunk():
    # 1300s of audio in 10s chunks; speech first appears at 1200s (chunk 120).
    chunks = _ten_second_chunks(130)
    idx = leading_silence_trim_index(
        chunks, speech_start_time=1200.0, min_trim_seconds=30.0
    )
    assert idx == 120


def test_short_leading_silence_is_not_trimmed():
    # A few seconds of pre-roll is fine — don't churn the conversation for it.
    chunks = _ten_second_chunks(10)
    idx = leading_silence_trim_index(
        chunks, speech_start_time=8.0, min_trim_seconds=30.0
    )
    assert idx is None


def test_speech_in_first_chunk_is_not_trimmed():
    # Even past the min threshold, if speech lands in chunk 0 there's nothing to trim.
    chunks = _ten_second_chunks(10)
    idx = leading_silence_trim_index(
        chunks, speech_start_time=0.0, min_trim_seconds=30.0
    )
    assert idx is None
