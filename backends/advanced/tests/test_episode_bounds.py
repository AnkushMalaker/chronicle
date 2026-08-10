"""Bounding an over-long episode without butchering a dense one.

The failure being fixed is a 956-minute episode. The failure being *avoided* is the
one this codebase already hit for recordings: cutting at a fixed length severed 94 of
176 capture windows mid-sentence.
"""

from datetime import datetime, timedelta, timezone

import pytest

from advanced_omi_backend.services.timeline.episode_bounds import (
    BUCKET_SECONDS,
    EPISODE_TARGET,
    BoundsVerdict,
    SpeechBucket,
    SpeechProfile,
    assess_profile,
)

T0 = datetime(2026, 7, 28, 6, 0, 0, tzinfo=timezone.utc)


def _buckets(minutes: float, bucket: SpeechBucket) -> list[SpeechBucket]:
    return [bucket] * int(minutes * 60 / BUCKET_SECONDS)


def speech(minutes: float) -> list[SpeechBucket]:
    return _buckets(minutes, SpeechBucket.measured(1.0))


def silence(minutes: float) -> list[SpeechBucket]:
    return _buckets(minutes, SpeechBucket.measured(0.0))


def no_capture(minutes: float) -> list[SpeechBucket]:
    """Nothing was recorded here — knowledge, and a boundary candidate."""
    return _buckets(minutes, SpeechBucket.no_capture())


def unscored(minutes: float) -> list[SpeechBucket]:
    """Audio exists but nobody ran VAD on it — ignorance, and never a boundary."""
    return _buckets(minutes, SpeechBucket.unscored())


def assess(buckets: list[SpeechBucket], hours: float | None = None, **kwargs):
    profile = SpeechProfile(tuple(buckets), BUCKET_SECONDS)
    span = (
        timedelta(hours=hours)
        if hours is not None
        else timedelta(seconds=len(buckets) * BUCKET_SECONDS)
    )
    return assess_profile(T0, T0 + span, profile, **kwargs)


class TestBucketStates:
    def test_unscored_is_never_quiet(self):
        """``not None`` being True is what made ignorance read as silence."""
        assert SpeechBucket.unscored().is_quiet is False
        assert SpeechBucket.unscored().is_measured is False
        assert SpeechBucket.unscored().is_known is False

    def test_a_capture_gap_is_quiet_and_known(self):
        """Nothing recorded is a fact: no speech happened, so a boundary may go here."""
        assert SpeechBucket.no_capture().is_quiet is True
        assert SpeechBucket.no_capture().is_known is True
        # Still not *measured* — no VAD ran, there was simply nothing to run it on.
        assert SpeechBucket.no_capture().is_measured is False

    def test_measured_zero_is_quiet(self):
        assert SpeechBucket.measured(0.0).is_quiet is True
        assert SpeechBucket.measured(0.0).is_measured is True

    def test_measured_speech_is_neither(self):
        assert SpeechBucket.measured(0.4).is_quiet is False
        assert SpeechBucket.measured(0.4).is_measured is True


class TestWithinTarget:
    def test_a_short_episode_is_left_alone_however_dense(self):
        result = assess(speech(90))

        assert result.verdict is BoundsVerdict.WITHIN_TARGET
        assert result.cuts == []

    def test_the_target_boundary_itself_is_not_a_split(self):
        result = assess(silence(120))

        assert result.verdict is BoundsVerdict.WITHIN_TARGET

    def test_an_episode_with_no_audio_at_all_is_not_judged(self):
        """A three-hour anime viewing is one episode; there is no audio to gate on."""
        result = assess([], hours=3)

        assert result.verdict is BoundsVerdict.NO_AUDIO
        assert result.cuts == []


class TestSplitting:
    def test_a_long_episode_breaks_at_a_real_silence(self):
        result = assess(speech(120) + silence(20) + speech(120))

        assert result.verdict is BoundsVerdict.SPLIT
        assert len(result.cuts) == 1
        gap_start = T0 + timedelta(minutes=120)
        assert gap_start <= result.cuts[0] <= gap_start + timedelta(minutes=20)

    def test_a_dense_marathon_with_no_seam_stays_whole(self):
        """A four-hour meeting that never stops is one episode, not two."""
        buckets: list[SpeechBucket] = []
        while len(buckets) < int(4 * 3600 / BUCKET_SECONDS):
            # Talking throughout, with only short breaths between.
            buckets += speech(4.833) + silence(0.167)

        result = assess(buckets)

        assert result.verdict is BoundsVerdict.NO_SEAM
        assert result.cuts == []

    def test_a_short_pause_is_not_a_seam(self):
        """Two quiet minutes is someone thinking, not someone leaving."""
        result = assess(speech(120) + silence(2) + speech(120))

        assert result.verdict is BoundsVerdict.NO_SEAM

    def test_a_very_long_episode_can_break_more_than_once(self):
        result = assess((speech(115) + silence(10)) * 3)

        assert result.verdict is BoundsVerdict.SPLIT
        assert len(result.cuts) >= 2
        assert result.cuts == sorted(result.cuts)


class TestUnmeasuredAudio:
    def test_unscored_audio_is_reported_not_cut(self):
        """Nothing measured it, so no seam can be honest — the 17.5-hour lesson."""
        result = assess(unscored(360))

        assert result.verdict is BoundsVerdict.UNANALYZED
        assert result.cuts == []

    def test_an_episode_of_pure_capture_gap_is_not_cut_either(self):
        """A silent three-hour film produced no audio; halving it would be absurd."""
        result = assess(no_capture(360))

        assert result.verdict is BoundsVerdict.NO_AUDIO
        assert result.cuts == []

    def test_a_capture_gap_between_audio_is_a_boundary(self):
        """The recorder stopping for twenty minutes ends an activity."""
        result = assess(speech(120) + no_capture(20) + speech(120))

        assert result.verdict is BoundsVerdict.SPLIT
        gap_start = T0 + timedelta(minutes=120)
        assert gap_start <= result.cuts[0] <= gap_start + timedelta(minutes=20)

    def test_measured_silence_is_preferred_over_an_unscored_stretch(self):
        result = assess(
            speech(115) + silence(10) + speech(10) + unscored(10) + speech(115)
        )

        assert result.verdict is BoundsVerdict.SPLIT
        assert len(result.cuts) == 1
        quiet_start = T0 + timedelta(minutes=115)
        assert quiet_start <= result.cuts[0] <= quiet_start + timedelta(minutes=10)


class TestCoverage:
    def test_barely_scored_audio_is_not_reported_as_seamless(self):
        """No seam found in 8% coverage says nothing about the other 92%.

        Two real episodes of 402 and 379 minutes reached this on 8% and 9% coverage.
        The remedy is to run VAD, not to re-segment.
        """
        result = assess(speech(30) + unscored(330) + speech(30))

        assert result.verdict is BoundsVerdict.LOW_COVERAGE
        assert result.vad_suspect is False

    def test_well_covered_audio_still_reports_no_seam(self):
        result = assess(speech(200) + unscored(20) + speech(200))

        assert result.verdict is BoundsVerdict.NO_SEAM


class TestDiagnostics:
    def test_a_seam_far_from_the_target_is_still_found(self):
        """A banded search around the target cannot see this; the episode rule must.

        Measured on two real episodes of 303 and 305 minutes, each holding a usable
        gap that a length-seeking search skipped straight past.
        """
        result = assess(speech(40) + silence(9) + speech(251))

        assert result.verdict is BoundsVerdict.SPLIT
        assert len(result.cuts) == 1
        gap_start = T0 + timedelta(minutes=40)
        assert gap_start <= result.cuts[0] <= gap_start + timedelta(minutes=9)

    def test_a_seamless_marathon_is_flagged_as_vad_suspect(self):
        """Given working VAD, five quiet minutes in five hours is near-certain."""
        result = assess(speech(300))

        assert result.verdict is BoundsVerdict.NO_SEAM
        assert result.vad_suspect is True

    def test_a_merely_long_episode_is_not_flagged(self):
        result = assess(speech(150))

        assert result.verdict is BoundsVerdict.NO_SEAM
        assert result.vad_suspect is False

    def test_partial_coverage_is_reported_as_a_fraction(self):
        result = assess(speech(60) + unscored(60) + silence(60))

        assert result.profile.measured_fraction == pytest.approx(2 / 3)
        assert result.profile.known_fraction == pytest.approx(2 / 3)
        assert result.profile.speech_fraction == pytest.approx(0.5)
        assert result.profile.longest_quiet_seconds == pytest.approx(3600)

    def test_a_capture_gap_counts_as_known_but_not_measured(self):
        result = assess(speech(60) + no_capture(60) + speech(60))

        assert result.profile.measured_fraction == pytest.approx(2 / 3)
        assert result.profile.known_fraction == pytest.approx(1.0)

    def test_an_unanalyzed_episode_reports_zero_coverage(self):
        result = assess(unscored(100), hours=3)

        assert result.profile.measured_fraction == 0.0
        assert result.profile.speech_fraction == 0.0


def test_the_target_is_two_hours():
    """Encoded so a change to the constant is a deliberate, visible decision."""
    assert EPISODE_TARGET == timedelta(hours=2)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
