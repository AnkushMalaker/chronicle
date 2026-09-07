"""Anchoring transcript timestamps to wall-clock time.

The case worth protecting is the trimmed conversation: relative time stays contiguous
while wall-clock time jumps, so any single-anchor arithmetic is silently wrong. That
shape covers 32% of this deployment's conversations.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.services.transcript_time import (
    MAX_EXTRAPOLATION,
    AbsoluteSegment,
    AnchorMap,
    ChunkAnchor,
    RangeTranscript,
    place_segments,
    segments_in_range,
)

T0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def chunk(start: float, captured_at: datetime, duration: float = 10.0):
    return SimpleNamespace(
        start_time=start,
        end_time=start + duration,
        duration=duration,
        captured_at=captured_at,
    )


def anchors_from(chunks, conversation_id: str = "conv") -> AnchorMap:
    return AnchorMap(
        conversation_id=conversation_id,
        anchors=[
            ChunkAnchor(item.start_time, item.end_time, item.captured_at)
            for item in chunks
        ],
    )


def segment(start: float, end: float, text: str, speaker: str = "Speaker 0", **kwargs):
    return SimpleNamespace(
        start=start,
        end=end,
        text=text,
        speaker=speaker,
        identified_as=kwargs.get("identified_as"),
        segment_type=kwargs.get("segment_type", "speech"),
    )


def conversation(segments, conversation_id: str = "conv"):
    return SimpleNamespace(conversation_id=conversation_id, segments=segments)


class TestAnchorMap:
    def test_untrimmed_conversation_resolves_linearly(self):
        anchors = anchors_from(
            [chunk(i * 10.0, T0 + timedelta(seconds=i * 10)) for i in range(3)]
        )
        assert anchors.resolve(0.0) == T0
        assert anchors.resolve(15.0) == T0 + timedelta(seconds=15)
        assert anchors.resolve(29.5) == T0 + timedelta(seconds=29.5)

    def test_trimmed_conversation_jumps_at_the_seam(self):
        """Relative time stays contiguous across a trim; wall-clock time does not."""

        # Two surviving chunks, repacked to 0-10s and 10-20s relative, but an hour of
        # silence was removed between them.
        anchors = anchors_from(
            [chunk(0.0, T0), chunk(10.0, T0 + timedelta(hours=1, seconds=10))]
        )

        assert anchors.resolve(5.0) == T0 + timedelta(seconds=5)
        # The naive single-anchor answer would be T0 + 15s -- an hour wrong.
        assert anchors.resolve(15.0) == T0 + timedelta(hours=1, seconds=15)
        assert anchors.resolve(15.0) != T0 + timedelta(seconds=15)

    def test_unanchored_conversation_refuses_to_guess(self):
        anchors = AnchorMap(conversation_id="conv", anchors=[])
        assert not anchors
        assert anchors.resolve(5.0) is None

    def test_offset_far_beyond_the_audio_is_refused(self):
        anchors = anchors_from([chunk(0.0, T0)])
        overshoot = MAX_EXTRAPOLATION.total_seconds() + 60
        assert anchors.resolve(10.0 + overshoot) is None

    def test_small_overshoot_is_placed_from_the_last_chunk(self):
        """A provider emitting past the audio is real; dropping that speech is worse."""

        anchors = anchors_from([chunk(0.0, T0)])
        assert anchors.resolve(12.0) == T0 + timedelta(seconds=12)

    def test_span_covers_trimmed_audio_end_to_end(self):
        anchors = anchors_from(
            [chunk(0.0, T0), chunk(10.0, T0 + timedelta(hours=1, seconds=10))]
        )
        span = anchors.span
        assert span is not None
        start, end = span
        assert start == T0
        assert end == T0 + timedelta(hours=1, seconds=20)


class TestPlaceSegments:
    def test_segments_take_wall_clock_time_across_a_trim(self):
        anchors = anchors_from(
            [chunk(0.0, T0), chunk(10.0, T0 + timedelta(hours=1, seconds=10))]
        )
        placed = place_segments(
            conversation(
                [segment(1.0, 3.0, "before the gap"), segment(12.0, 14.0, "after it")]
            ),
            anchors,
        )
        assert [item.text for item in placed] == ["before the gap", "after it"]
        assert placed[0].started_at == T0 + timedelta(seconds=1)
        assert placed[1].started_at == T0 + timedelta(hours=1, seconds=12)

    def test_unplaceable_segments_are_dropped_not_guessed(self):
        anchors = anchors_from([chunk(0.0, T0)])
        far = 10.0 + MAX_EXTRAPOLATION.total_seconds() + 60
        placed = place_segments(
            conversation([segment(1.0, 2.0, "real"), segment(far, far + 1, "adrift")]),
            anchors,
        )
        assert [item.text for item in placed] == ["real"]

    def test_identified_speaker_wins_over_the_diarization_label(self):
        anchors = anchors_from([chunk(0.0, T0)])
        placed = place_segments(
            conversation([segment(1.0, 2.0, "hi", "Speaker 1", identified_as="Blair")]),
            anchors,
        )
        assert placed[0].label == "Blair"

    def test_no_anchors_yields_nothing(self):
        placed = place_segments(
            conversation([segment(1.0, 2.0, "hi")]),
            AnchorMap(conversation_id="conv", anchors=[]),
        )
        assert placed == []


class TestSegmentsInRange:
    def build(self):
        return [
            AbsoluteSegment("c", T0, T0 + timedelta(seconds=5), "first", "A"),
            AbsoluteSegment(
                "c",
                T0 + timedelta(minutes=10),
                T0 + timedelta(minutes=10, seconds=5),
                "middle",
                "A",
            ),
            AbsoluteSegment(
                "c",
                T0 + timedelta(minutes=40),
                T0 + timedelta(minutes=40, seconds=5),
                "last",
                "A",
            ),
        ]

    def test_only_overlapping_segments_survive(self):
        kept = segments_in_range(
            self.build(), T0 + timedelta(minutes=5), T0 + timedelta(minutes=20)
        )
        assert [item.text for item in kept] == ["middle"]

    def test_a_segment_straddling_the_boundary_is_kept(self):
        segments = [
            AbsoluteSegment("c", T0, T0 + timedelta(minutes=30), "straddles", "A")
        ]
        kept = segments_in_range(
            segments, T0 + timedelta(minutes=10), T0 + timedelta(minutes=20)
        )
        assert len(kept) == 1

    def test_touching_the_boundary_does_not_count_as_overlap(self):
        segments = [AbsoluteSegment("c", T0, T0 + timedelta(minutes=10), "before", "A")]
        assert (
            segments_in_range(
                segments, T0 + timedelta(minutes=10), T0 + timedelta(minutes=20)
            )
            == []
        )


class TestRangeTranscript:
    def test_render_is_speaker_attributed_and_wall_clock_stamped(self):
        result = RangeTranscript(
            started_at=T0,
            ended_at=T0 + timedelta(minutes=1),
            segments=[
                AbsoluteSegment(
                    "c", T0, T0 + timedelta(seconds=2), "hello", "Speaker 0"
                ),
                AbsoluteSegment(
                    "c",
                    T0 + timedelta(seconds=3),
                    T0 + timedelta(seconds=5),
                    "hi back",
                    "Speaker 1",
                    identified_as="Blair",
                ),
            ],
        )
        assert result.render() == (
            "[12:00:00] Speaker 0: hello\n[12:00:03] Blair: hi back"
        )

    def test_render_uses_the_requested_timezone(self):
        result = RangeTranscript(
            started_at=T0,
            ended_at=T0 + timedelta(minutes=1),
            segments=[AbsoluteSegment("c", T0, T0 + timedelta(seconds=2), "hi", "A")],
        )
        assert result.render("Asia/Kolkata").startswith("[17:30:00]")

    def test_unknown_timezone_falls_back_to_utc_rather_than_failing(self):
        result = RangeTranscript(
            started_at=T0,
            ended_at=T0 + timedelta(minutes=1),
            segments=[AbsoluteSegment("c", T0, T0 + timedelta(seconds=2), "hi", "A")],
        )
        assert result.render("Not/AZone").startswith("[12:00:00]")

    def test_empty_range_is_reported_as_empty(self):
        assert RangeTranscript(started_at=T0, ended_at=T0).is_empty


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
