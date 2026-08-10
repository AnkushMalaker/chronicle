"""Where a continuous-capture window is cut into recordings.

The rule this replaces cut at exactly 30 minutes regardless of what was being said.
Measured over this deployment's ScreenPipe corpus, 176 of 237 recordings hit that cap
and 94 of them had speech running to within 15 seconds of the cut.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from advanced_omi_backend.services.device_audio_ingest import (
    plan_session_cuts,
    split_window,
)

BUCKET = 10.0
START = datetime(2026, 8, 7, 18, 17, 59, tzinfo=timezone.utc)


def source_item(index, duration=30):
    at = START + timedelta(seconds=index * duration)
    return SimpleNamespace(
        source_item_id=str(index),
        captured_at=at,
        ended_at=at + timedelta(seconds=duration),
        metadata={},
    )


def buckets(minutes, speech=False):
    return [1.0 if speech else 0.0] * int(minutes * 60 / BUCKET)


def test_a_window_inside_the_target_is_not_cut():
    assert plan_session_cuts(buckets(25, speech=True), BUCKET) == []


def test_a_quiet_window_is_cut_near_the_target():
    cuts = plan_session_cuts(buckets(60), BUCKET)

    # All quiet, so the longest run spans the whole search band and the cut lands in
    # its middle — inside the 15-45 minute band around the 30-minute target.
    assert len(cuts) == 1
    assert 900 <= cuts[0] <= 2700


def test_the_cut_avoids_speech_that_is_still_going():
    """The 59e16f4f failure: speech ran right up to the 30-minute mark."""
    # Quiet for 20 minutes, then talking from 20 to 40 minutes, then quiet again.
    timeline = buckets(20) + buckets(20, speech=True) + buckets(20)

    cuts = plan_session_cuts(timeline, BUCKET)

    # The old rule cut at 1800s — squarely inside the conversation. Every cut now
    # falls outside it.
    assert cuts
    for cut in cuts:
        assert not (1200 < cut < 2400), f"cut at {cut}s severs the conversation"


def test_a_dense_call_under_the_safety_cap_stays_one_recording():
    """The Vatsal 1-1: 63 minutes at 66% speech, stored as 30 + 30 + 3.

    There is no good seam in it, so the honest answer is one 63-minute recording. An
    earlier version took the longest quiet run *anywhere* when it found none near the
    target, which cut at 62.8 minutes and left a 12-second stub.
    """
    # Talking throughout, with only short breaths between.
    timeline = []
    while len(timeline) < 378:
        timeline += [1.0] * 29 + [0.0]

    assert plan_session_cuts(timeline, BUCKET) == []


def test_a_cut_needs_a_real_silence_not_a_breath():
    """A 10-second gap is a pause in a sentence, not the end of a conversation."""
    timeline = [1.0] * 120 + [0.0] + [1.0] * 260

    assert plan_session_cuts(timeline, BUCKET) == []


def test_continuous_speech_is_still_cut_at_the_safety_cap():
    """A window that genuinely never stops has to be cut somewhere."""
    cuts = plan_session_cuts(buckets(150, speech=True), BUCKET)

    assert cuts == [7200.0]


def test_unscored_buckets_count_as_quiet_rather_than_blocking_a_cut():
    """A VAD with no verdict must not force an arbitrary cut."""
    timeline = buckets(20, speech=True) + [None] * 120 + buckets(20, speech=True)

    cuts = plan_session_cuts(timeline, BUCKET)

    # The unscored stretch runs 1200-2400s and is the only non-speech region.
    assert len(cuts) == 1
    assert 1200 <= cuts[0] <= 2400


def test_a_window_nothing_was_measured_in_is_left_whole():
    """An unscored window is uniformly *unknown*, which is not the same as silent.

    Reading it as silent makes the longest quiet run the whole window, so the cut
    lands on the blind target this function exists to remove — and the failure hides,
    because such a window also reports as carrying no speech at all. Measured during
    the corpus re-bound: 18 windows, 17.5 hours, every cut at exactly 30:00.
    """
    assert plan_session_cuts([None] * 1080, BUCKET) == []


def test_measured_silence_is_preferred_over_an_unmeasured_stretch():
    """Cut where the audio is known to be quiet, not merely where nothing is known."""
    # Real silence at 1500-1800s; an unscored stretch at 2100-2400s.
    timeline = (
        buckets(25, speech=True)
        + [0.0] * 30
        + buckets(5, speech=True)
        + [None] * 30
        + buckets(25, speech=True)
    )

    cuts = plan_session_cuts(timeline, BUCKET)

    assert len(cuts) == 1
    assert 1500 <= cuts[0] <= 1800


def test_a_long_window_is_cut_more_than_once():
    cuts = plan_session_cuts(buckets(120), BUCKET)

    assert len(cuts) >= 2
    assert cuts == sorted(cuts)


def test_an_empty_profile_plans_nothing():
    assert plan_session_cuts([], BUCKET) == []
    assert plan_session_cuts(buckets(60), 0.0) == []


def test_every_source_file_lands_in_exactly_one_recording():
    """A cut inside a 30-second source file must not duplicate it into both sides.

    Duplicating would store the audio twice and collide on the evidence span's
    (first item, last item) uniqueness key.
    """
    items = [source_item(index) for index in range(120)]  # 60 minutes

    parts = split_window(items, START, [1805.0])

    assigned = [item.source_item_id for part, _, _ in parts for item in part]
    assert sorted(assigned, key=int) == [str(i) for i in range(120)]
    assert len(assigned) == len(set(assigned))


def test_a_window_with_no_cuts_is_one_recording():
    items = [source_item(index) for index in range(60)]

    parts = split_window(items, START, [])

    assert len(parts) == 1
    assert parts[0][1] == 0.0


def test_the_cut_time_bounds_each_part():
    items = [source_item(index) for index in range(120)]

    parts = split_window(items, START, [1800.0, 2400.0])

    assert [(low, high) for _, low, high in parts] == [
        (0.0, 1800.0),
        (1800.0, 2400.0),
        (2400.0, float("inf")),
    ]
