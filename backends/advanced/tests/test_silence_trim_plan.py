"""The pure trim decision: which chunks survive, and how time maps.

``plan_silence_trim`` is the heart of the holistic fix. Given a chunk timeline and
where speech actually is, it decides what the conversation keeps — and produces the
map that re-times the transcript so it still addresses the audio it names.
"""

import pytest

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.utils.audio_trim import (
    plan_silence_trim,
    remap_segments,
    remap_words,
)


def chunks(count, size=10.0):
    return [
        {
            "chunk_index": i,
            "start_time": i * size,
            "end_time": (i + 1) * size,
            "duration": size,
        }
        for i in range(count)
    ]


def test_a_recording_with_speech_only_at_the_end_keeps_only_the_end():
    """Conversation 59e16f4f: 30 minutes stored, speech in the last 148 seconds."""
    plan = plan_silence_trim(chunks(180), [(1652.0, 1800.0)])

    assert plan.trims
    # 1652 padded to 1647 → the chunk starting at 1640 is the first survivor.
    assert plan.keep == list(range(164, 180))
    assert plan.kept_seconds == pytest.approx(160.0)
    assert plan.dropped_seconds == pytest.approx(1640.0)
    # One surviving run, packed to the front of the new timeline.
    assert plan.regions == [(1640.0, 1800.0, 0.0)]


def test_speech_islands_each_survive_with_their_padding():
    plan = plan_silence_trim(chunks(180), [(1000.0, 1030.0), (1500.0, 1530.0)])

    assert plan.regions == [(990.0, 1040.0, 0.0), (1490.0, 1540.0, 50.0)]
    assert plan.kept_seconds == pytest.approx(100.0)


def test_silence_shorter_than_a_cuttable_run_survives_even_next_to_speech():
    """The min-run rule applies to leading silence too, not just interior gaps."""
    plan = plan_silence_trim(chunks(180), [(100.0, 130.0), (1500.0, 1530.0)])

    # The 90s before the first speech is below min_run_seconds, so it is kept and
    # merges into the first region rather than being cut off the front.
    assert plan.regions == [(0.0, 140.0, 0.0), (1490.0, 1540.0, 140.0)]


def test_a_pause_shorter_than_a_cuttable_run_is_kept():
    """A conversational pause must not become a cut, or playback starts skipping."""
    plan = plan_silence_trim(chunks(60), [(0.0, 100.0), (200.0, 600.0)])

    # The 100s gap is below min_run_seconds, so it stays in the conversation.
    assert not plan.trims
    assert plan.keep == list(range(60))


def test_a_trim_too_small_to_matter_is_declined():
    # A cuttable 120s run exists, but 120s is not worth re-timing a whole recording
    # for when the floor is set higher.
    plan = plan_silence_trim(
        chunks(60),
        [(0.0, 400.0), (530.0, 600.0)],
        min_run_seconds=60.0,
        min_saving_seconds=180.0,
    )
    assert not plan.trims


def test_silence_with_no_speech_at_all_is_not_a_trim_decision():
    """Emptying a conversation is the speech gate's call, not the trim's."""
    plan = plan_silence_trim(chunks(180), [])
    assert plan.keep == list(range(180))
    assert not plan.trims


def test_the_transcript_moves_with_the_audio():
    plan = plan_silence_trim(chunks(180), [(1000.0, 1030.0), (1500.0, 1530.0)])
    segments = [
        Conversation.SpeakerSegment(
            speaker="a", start=1005.0, end=1025.0, text="first"
        ),
        Conversation.SpeakerSegment(
            speaker="a", start=1505.0, end=1525.0, text="second"
        ),
    ]

    remapped = remap_segments(segments, plan.regions)

    # Region one starts at 990 and lands at 0; region two starts at 1490 and lands at 50.
    assert [(s.start, s.end) for s in remapped] == [(15.0, 35.0), (65.0, 85.0)]
    assert [s.text for s in remapped] == ["first", "second"]


def test_a_segment_inside_cut_audio_is_dropped_not_misplaced():
    """Better to lose a stray segment than to have it point at unrelated audio."""
    plan = plan_silence_trim(chunks(180), [(1000.0, 1030.0), (1500.0, 1530.0)])
    segments = [
        Conversation.SpeakerSegment(speaker="a", start=200.0, end=220.0, text="cut"),
    ]

    assert remap_segments(segments, plan.regions) == []


def test_words_move_with_their_segment():
    plan = plan_silence_trim(chunks(180), [(1000.0, 1030.0)])
    words = [
        Conversation.Word(word="hello", start=1005.0, end=1005.5),
        Conversation.Word(word="there", start=1006.0, end=1006.5),
    ]

    remapped = remap_words(words, plan.regions)

    assert [(w.start, w.end) for w in remapped] == [(15.0, 15.5), (16.0, 16.5)]


def test_a_partial_final_chunk_keeps_its_true_length():
    timeline = chunks(9)
    timeline.append(
        {"chunk_index": 9, "start_time": 90.0, "end_time": 93.5, "duration": 3.5}
    )
    plan = plan_silence_trim(
        timeline, [(85.0, 93.5)], min_run_seconds=30.0, min_saving_seconds=10.0
    )

    # Chunk 7 (70-80s) holds none of the padded speech, so the survivors are 8 and 9 —
    # and the final one contributes its real 3.5s, not a nominal 10.
    assert plan.regions[-1][1] == 93.5
    assert plan.kept_seconds == pytest.approx(13.5)


def test_an_empty_conversation_produces_no_plan():
    plan = plan_silence_trim([], [(0.0, 10.0)])
    assert not plan.trims
    assert plan.keep == []
