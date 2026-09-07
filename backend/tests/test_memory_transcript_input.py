"""Memory extraction input must not amplify overlapping transcript windows."""

from types import SimpleNamespace

from backend.workers.memory_jobs import build_memory_transcript


def _segment(start, end, text, speaker="Speaker 0", segment_type="speech"):
    return SimpleNamespace(
        start=start,
        end=end,
        text=text,
        speaker=speaker,
        segment_type=segment_type,
    )


def test_build_memory_transcript_trims_overlapping_word_prefix():
    segments = [
        _segment(0, 30, "one two three four five"),
        _segment(25, 55, "three four five six seven", "Speaker 1"),
    ]

    transcript, speakers = build_memory_transcript(segments, raw_transcript=None)

    assert transcript == "Speaker 0: one two three four five\nSpeaker 1: six seven"
    assert speakers == {"speaker 0", "speaker 1"}


def test_build_memory_transcript_falls_back_when_segments_amplify_raw_text():
    segments = [
        _segment(0, 100, "duplicated window text " * 100),
        _segment(50, 150, "different duplicated text " * 100),
    ]
    raw = "This is the durable raw transcript and it should be used instead."

    transcript, speakers = build_memory_transcript(segments, raw_transcript=raw)

    assert transcript == raw
    assert speakers == {"speaker 0"}


def test_build_memory_transcript_preserves_events_and_notes():
    segments = [
        _segment(0, 1, "music", segment_type="event"),
        _segment(1, 2, "Remember this", segment_type="note"),
    ]

    transcript, speakers = build_memory_transcript(segments, raw_transcript=None)

    assert transcript == "[music]\n[Note: Remember this]"
    assert speakers == set()
