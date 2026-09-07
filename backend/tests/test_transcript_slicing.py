"""Unit tests for transcript slicing/shifting used by conversation split/merge."""

from backend.models.conversation import Conversation
from backend.utils.transcript_slicing import (
    build_transcript_text,
    shift_segments,
    shift_words,
    slice_segments,
    slice_words,
)


def word(text, start, end):
    return Conversation.Word(word=text, start=start, end=end)


def segment(text, start, end, speaker="Speaker 1", segment_type="speech", words=None):
    return Conversation.SpeakerSegment(
        start=start,
        end=end,
        text=text,
        speaker=speaker,
        segment_type=segment_type,
        words=words or [],
    )


class TestSliceWords:
    def test_membership_and_shift(self):
        words = [word("a", 5.0, 5.5), word("b", 10.0, 10.5), word("c", 20.0, 20.5)]
        sliced = slice_words(words, 10.0, 20.0)
        assert [w.word for w in sliced] == ["b"]
        assert sliced[0].start == 0.0
        assert sliced[0].end == 0.5

    def test_end_clamped_to_range(self):
        words = [word("a", 19.5, 21.0)]
        sliced = slice_words(words, 10.0, 20.0)
        assert sliced[0].start == 9.5
        assert sliced[0].end == 10.0


class TestSliceSegments:
    def test_midpoint_membership(self):
        segments = [
            segment("before", 0.0, 8.0),
            segment("inside", 12.0, 18.0),
            segment("after", 22.0, 30.0),
        ]
        sliced = slice_segments(segments, 10.0, 20.0)
        assert [s.text for s in sliced] == ["inside"]
        assert sliced[0].start == 2.0
        assert sliced[0].end == 8.0

    def test_clamps_overhanging_segment(self):
        # Midpoint at 15 → included; start before range start gets clamped
        segments = [segment("overhang", 8.0, 22.0)]
        sliced = slice_segments(segments, 10.0, 20.0)
        assert sliced[0].start == 0.0
        assert sliced[0].end == 10.0

    def test_nested_words_sliced_and_shifted(self):
        words = [word("in", 12.0, 12.5), word("out", 25.0, 25.5)]
        segments = [segment("seg", 11.0, 19.0, words=words)]
        sliced = slice_segments(segments, 10.0, 20.0)
        assert len(sliced[0].words) == 1
        assert sliced[0].words[0].word == "in"
        assert sliced[0].words[0].start == 2.0

    def test_speaker_labels_preserved(self):
        segments = [segment("hello", 12.0, 14.0, speaker="Speaker 2")]
        sliced = slice_segments(segments, 10.0, 20.0)
        assert sliced[0].speaker == "Speaker 2"


class TestShift:
    def test_shift_segments_with_words(self):
        segments = [segment("s", 0.0, 5.0, words=[word("w", 1.0, 1.5)])]
        shifted = shift_segments(segments, 100.0)
        assert shifted[0].start == 100.0
        assert shifted[0].end == 105.0
        assert shifted[0].words[0].start == 101.0

    def test_shift_words(self):
        shifted = shift_words([word("w", 1.0, 1.5)], 10.0)
        assert shifted[0].start == 11.0
        assert shifted[0].end == 11.5

    def test_originals_not_mutated(self):
        seg = segment("s", 0.0, 5.0)
        shift_segments([seg], 100.0)
        assert seg.start == 0.0


class TestBuildTranscriptText:
    def test_joins_speech_skips_notes_and_events(self):
        segments = [
            segment("Hello there.", 0.0, 2.0),
            segment(
                "[merged: 20 min gap elided]",
                2.0,
                2.0,
                speaker="system",
                segment_type="note",
            ),
            segment("[laughter]", 3.0, 4.0, segment_type="event"),
            segment("Bye.", 5.0, 6.0),
        ]
        assert build_transcript_text(segments) == "Hello there. Bye."

    def test_empty(self):
        assert build_transcript_text([]) == ""
