"""
Tests for audio batching and transcript stitching.

Two categories:
1. Unit tests for stitching logic (no GPU needed, always run)
2. GPU integration test comparing batched vs direct transcription (requires GPU + model)

Run unit tests:
    cd extras/asr-services
    uv run pytest tests/test_batching.py -v -k "not gpu"

Run GPU tests:
    cd extras/asr-services
    RUN_GPU_TESTS=1 uv run pytest tests/test_batching.py -v
"""

import difflib
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

# Add the asr-services root to path so common/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.batching import (
    _clip_segments,
    _clip_words,
    extract_context_tail,
    split_audio_file,
    stitch_transcription_results,
)
from common.response_models import Segment, Speaker, TranscriptionResult, Word

# ---------------------------------------------------------------------------
# Unit tests for stitching logic (no GPU)
# ---------------------------------------------------------------------------


def _make_result(segments, words=None, text=None):
    """Helper to build a TranscriptionResult from simple data."""
    seg_objs = [
        Segment(text=s[0], start=s[1], end=s[2], speaker=s[3] if len(s) > 3 else None)
        for s in segments
    ]
    word_objs = [Word(word=w[0], start=w[1], end=w[2]) for w in (words or [])]
    return TranscriptionResult(
        text=text or " ".join(s.text for s in seg_objs),
        words=word_objs,
        segments=seg_objs,
    )


class TestStitchNoOverlap:
    """Stitching non-overlapping batches should concatenate cleanly."""

    def test_single_batch(self):
        result = _make_result([("hello world", 0.0, 3.0)])
        stitched = stitch_transcription_results([(result, 0.0, 3.0)], overlap_seconds=0)

        assert len(stitched.segments) == 1
        assert stitched.segments[0].text == "hello world"
        assert stitched.segments[0].start == 0.0

    def test_two_batches_no_overlap(self):
        r1 = _make_result([("first part", 0.0, 5.0)])
        r2 = _make_result([("second part", 0.0, 5.0)])

        stitched = stitch_transcription_results(
            [(r1, 0.0, 5.0), (r2, 5.0, 10.0)],
            overlap_seconds=0,
        )

        assert len(stitched.segments) == 2
        assert stitched.segments[0].text == "first part"
        assert stitched.segments[0].start == 0.0
        assert stitched.segments[1].text == "second part"
        assert stitched.segments[1].start == 5.0

    def test_empty_input(self):
        stitched = stitch_transcription_results([], overlap_seconds=0)
        assert stitched.text == ""
        assert len(stitched.segments) == 0


class TestStitchWithOverlap:
    """Overlapping segments should be deduplicated using midpoint strategy."""

    def test_overlap_deduplication(self):
        # Batch 1: [0-70s] with segments throughout
        r1 = _make_result(
            [
                ("seg A", 0.0, 20.0),
                ("seg B", 20.0, 40.0),
                ("seg C", 40.0, 60.0),  # overlap region: 50-70
                (
                    "seg D",
                    60.0,
                    70.0,
                ),  # midpoint=65, overlap midpoint=50+10/2=55 -> 65 >= 55? yes for batch 1 cutoff
            ]
        )

        # Batch 2: [50-120s] with segments throughout
        r2 = _make_result(
            [
                ("seg C'", 0.0, 10.0),  # absolute: 50-60, midpoint=55 >= cutoff
                ("seg D'", 10.0, 20.0),  # absolute: 60-70, midpoint=65 >= cutoff
                ("seg E", 20.0, 40.0),  # absolute: 70-90
                ("seg F", 40.0, 70.0),  # absolute: 90-120
            ]
        )

        stitched = stitch_transcription_results(
            [(r1, 0.0, 70.0), (r2, 50.0, 120.0)],
            overlap_seconds=20.0,
        )

        # Overlap midpoint = 50 + 20/2 = 60
        # From r1: keep segments with midpoint < 60 → seg A (10), seg B (30), seg C (50) - yes
        # From r1: seg D midpoint = 65 >= 60 → excluded
        # From r2: keep segments with midpoint >= 60 → C' (55) no, D' (65) yes, E (80) yes, F (105) yes
        texts = [s.text for s in stitched.segments]
        assert "seg A" in texts
        assert "seg B" in texts
        assert "seg C" in texts
        assert "seg D'" in texts
        assert "seg E" in texts
        assert "seg F" in texts

    def test_three_batches_with_overlap(self):
        r1 = _make_result([("a", 0.0, 50.0), ("b", 50.0, 90.0)])
        r2 = _make_result([("b'", 0.0, 20.0), ("c", 20.0, 60.0), ("d", 60.0, 90.0)])
        r3 = _make_result([("d'", 0.0, 20.0), ("e", 20.0, 50.0)])

        stitched = stitch_transcription_results(
            [(r1, 0.0, 90.0), (r2, 70.0, 160.0), (r3, 140.0, 190.0)],
            overlap_seconds=20.0,
        )

        # All segments should have absolute timestamps
        assert stitched.segments[0].start == 0.0
        assert stitched.duration > 0


class TestExtractContextTail:
    """Should extract last N chars from segments."""

    def test_basic_extraction(self):
        result = _make_result([("hello world", 0.0, 3.0)])
        tail = extract_context_tail(result, max_chars=5)
        assert tail == "world"

    def test_full_text_when_short(self):
        result = _make_result([("hi", 0.0, 1.0)])
        tail = extract_context_tail(result, max_chars=500)
        assert tail == "hi"

    def test_empty_result(self):
        result = TranscriptionResult(text="", words=[], segments=[])
        tail = extract_context_tail(result)
        assert tail == ""

    def test_multiple_segments(self):
        result = _make_result(
            [
                ("first segment", 0.0, 5.0),
                ("second segment", 5.0, 10.0),
            ]
        )
        tail = extract_context_tail(result, max_chars=20)
        assert "second segment" in tail


class TestSplitAudioFile:
    """Test audio file splitting into windows."""

    def _make_test_wav(self, duration_seconds: float, sample_rate: int = 16000) -> str:
        """Create a temp WAV file with sine wave audio."""
        samples = int(duration_seconds * sample_rate)
        t = np.linspace(0, duration_seconds, samples, dtype=np.float32)
        audio = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)

        # Convert to int16
        audio_int16 = (audio * 32767).astype(np.int16)

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int16.tobytes())
        return tmp.name

    def test_short_audio_single_window(self):
        """Audio shorter than batch_duration should produce one window."""
        wav_path = self._make_test_wav(30.0)
        try:
            windows = split_audio_file(wav_path, batch_duration=60.0, overlap=10.0)
            assert len(windows) == 1
            path, start, end = windows[0]
            assert start == 0.0
            assert abs(end - 30.0) < 0.1
            os.unlink(path)
        finally:
            os.unlink(wav_path)

    def test_long_audio_multiple_windows(self):
        """12-minute audio with 4-min batches should produce 3 windows."""
        wav_path = self._make_test_wav(720.0)  # 12 minutes
        try:
            windows = split_audio_file(wav_path, batch_duration=240.0, overlap=30.0)
            assert len(windows) == 3

            # Window 0: [0, 270]
            assert windows[0][1] == 0.0
            assert abs(windows[0][2] - 270.0) < 0.1

            # Window 1: [240, 510]
            assert abs(windows[1][1] - 240.0) < 0.1
            assert abs(windows[1][2] - 510.0) < 0.1

            # Window 2: [480, 720]
            assert abs(windows[2][1] - 480.0) < 0.1
            assert abs(windows[2][2] - 720.0) < 0.1

            # Clean up temp files
            for path, _, _ in windows:
                os.unlink(path)
        finally:
            os.unlink(wav_path)

    def test_windows_are_valid_wav(self):
        """Each window should be a valid WAV file."""
        wav_path = self._make_test_wav(120.0)
        try:
            windows = split_audio_file(wav_path, batch_duration=60.0, overlap=10.0)
            for path, start, end in windows:
                with wave.open(path, "rb") as wf:
                    assert wf.getnchannels() == 1
                    assert wf.getsampwidth() == 2
                    assert wf.getframerate() == 16000
                    duration = wf.getnframes() / wf.getframerate()
                    expected = end - start
                    assert abs(duration - expected) < 0.1
                os.unlink(path)
        finally:
            os.unlink(wav_path)


class TestBoundaryClipping:
    """Segments are clipped at batch cutoff boundaries, not against each other.

    Cross-batch bleeding (a segment from batch N extending into batch N+1's
    territory) must be eliminated.  Legitimate within-batch overlaps (e.g.
    Music + Speech happening simultaneously) must be preserved.
    """

    def test_cross_batch_segment_clipped_at_cutoff(self):
        """Segment whose end bleeds past the cutoff gets trimmed to the cutoff."""
        # Batches: [0-270] and [240-510], overlap=30, cutoff=255
        r1 = _make_result(
            [
                ("speech A", 0.0, 100.0, "Speaker 0"),
                ("[Environmental Sounds]", 200.0, 270.0, ""),  # bleeds past 255
            ]
        )
        r2 = _make_result(
            [
                (
                    "[Music]",
                    1.0,
                    40.0,
                    "",
                ),  # abs 241-280, midpoint 260.5 >= 255 → kept, start clipped to 255
                ("speech B", 40.0, 100.0, "Speaker 1"),  # abs 280-340
            ]
        )
        stitched = stitch_transcription_results(
            [(r1, 0.0, 270.0), (r2, 240.0, 510.0)],
            overlap_seconds=30.0,
        )
        env = next(s for s in stitched.segments if s.text == "[Environmental Sounds]")
        music = next(s for s in stitched.segments if s.text == "[Music]")
        # Clipped at cutoff — no cross-batch overlap
        assert env.end == pytest.approx(255.0)
        assert music.start == pytest.approx(255.0)

    def test_real_scenario_human_sounds_music(self):
        """Reproduce the actual bug: [Human Sounds] and [Music] at batch boundary."""
        # Batches: [1200-1470] and [1440-1710], overlap=30, cutoff=1455
        r_mid = _make_result(
            [
                ("speech X", 0.0, 20.0, "Speaker 0"),
                (
                    "[Human Sounds]",
                    218.0,
                    270.0,
                    "",
                ),  # abs 1418-1470, midpoint 1444 < 1455
            ]
        )
        r_next = _make_result(
            [
                ("[Music]", 1.0, 38.0, ""),  # abs 1441-1478, midpoint 1459.5 >= 1455
                ("speech Y", 38.0, 100.0, "Speaker 1"),
            ]
        )
        stitched = stitch_transcription_results(
            [(r_mid, 1200.0, 1470.0), (r_next, 1440.0, 1710.0)],
            overlap_seconds=30.0,
        )
        human = next(s for s in stitched.segments if s.text == "[Human Sounds]")
        music = next(s for s in stitched.segments if s.text == "[Music]")
        assert human.end == pytest.approx(1455.0)
        assert music.start == pytest.approx(1455.0)
        assert human.end <= music.start + 0.01

    def test_within_batch_overlaps_preserved(self):
        """Concurrent segments within the same batch (Music + Speech) must survive."""
        # Batches: [0-270] and [240-510], overlap=30, cutoff=255
        r1 = _make_result(
            [
                ("[Music]", 0.0, 30.0, ""),
                (
                    "speech over music",
                    5.0,
                    15.0,
                    "Speaker 0",
                ),  # overlaps Music — legitimate
                ("later speech", 100.0, 200.0, "Speaker 0"),
            ]
        )
        r2 = _make_result([("batch2 speech", 20.0, 50.0, "Speaker 1")])  # abs 260-290

        stitched = stitch_transcription_results(
            [(r1, 0.0, 270.0), (r2, 240.0, 510.0)],
            overlap_seconds=30.0,
        )
        texts = [s.text for s in stitched.segments]
        assert "[Music]" in texts
        assert "speech over music" in texts
        # The within-batch overlap is intact
        music = next(s for s in stitched.segments if s.text == "[Music]")
        speech = next(s for s in stitched.segments if s.text == "speech over music")
        assert speech.start < music.end  # they still overlap
        assert music.start == 0.0
        assert speech.start == 5.0
        assert speech.end == 15.0

    def test_within_batch_overlaps_near_boundary_preserved(self):
        """Within-batch overlaps near the cutoff survive — clipped against boundary, not each other."""
        # Batches: [0-270] and [240-510], overlap=30, cutoff=255
        r1 = _make_result(
            [
                ("early", 0.0, 50.0, "Speaker 0"),
                ("[Music]", 240.0, 260.0, ""),  # midpoint 250 < 255, end clipped to 255
                (
                    "speech over music",
                    245.0,
                    253.0,
                    "Speaker 0",
                ),  # midpoint 249 < 255, fully within
            ]
        )
        r2 = _make_result([("batch2", 20.0, 50.0, "Speaker 1")])  # abs 260-290

        stitched = stitch_transcription_results(
            [(r1, 0.0, 270.0), (r2, 240.0, 510.0)],
            overlap_seconds=30.0,
        )
        music = next(s for s in stitched.segments if s.text == "[Music]")
        speech = next(s for s in stitched.segments if s.text == "speech over music")
        # Music gets clipped at cutoff, but speech is fully inside — overlap preserved
        assert music.end == pytest.approx(255.0)
        assert speech.start == 245.0
        assert speech.end == 253.0
        assert speech.start < music.end  # overlap still exists

    def test_three_batches_clips_both_sides(self):
        """Middle batch gets clipped on both left and right boundaries."""
        # Batches: [0-90], [70-160], [140-190], overlap=20
        # Cutoffs: left=80 (70+10), right=150 (140+10)
        r1 = _make_result(
            [
                ("a", 0.0, 50.0, "S0"),
                ("b", 50.0, 90.0, "S0"),
            ]  # b: abs 50-90, mid 70 < 80
        )
        r2 = _make_result(
            [
                (
                    "b'",
                    0.0,
                    20.0,
                    "S0",
                ),  # abs 70-90, mid 80 >= left 80 → kept, start clipped to 80
                ("c", 20.0, 60.0, "S1"),  # abs 90-130, fully inside
                (
                    "d",
                    60.0,
                    90.0,
                    "S0",
                ),  # abs 130-160, mid 145 < right 150, end clipped to 150
            ]
        )
        r3 = _make_result(
            [
                ("d'", 0.0, 20.0, "S0"),
                ("e", 20.0, 50.0, "S1"),
            ]  # d': abs 140-160, mid 150 >= 150
        )
        stitched = stitch_transcription_results(
            [(r1, 0.0, 90.0), (r2, 70.0, 160.0), (r3, 140.0, 190.0)],
            overlap_seconds=20.0,
        )
        # b from r1: mid 70 < 80 → kept, end clipped to 80
        seg_b = next(s for s in stitched.segments if s.text == "b")
        assert seg_b.end == pytest.approx(80.0)
        # b' from r2: mid 80 >= 80 → kept, start clipped to 80
        seg_b2 = next(s for s in stitched.segments if s.text == "b'")
        assert seg_b2.start == pytest.approx(80.0)
        # d from r2: mid 145 < 150 → kept, end clipped to 150
        seg_d = next(s for s in stitched.segments if s.text == "d")
        assert seg_d.end == pytest.approx(150.0)
        # d' from r3: mid 150 >= 150 → kept, start clipped to 150
        seg_d2 = next(s for s in stitched.segments if s.text == "d'")
        assert seg_d2.start == pytest.approx(150.0)

    def test_words_clipped_at_boundary(self):
        """Word timestamps are also clipped at the cutoff, not against each other."""
        # Batches: [0-270] and [240-510], overlap=30, cutoff=255
        r1 = _make_result(
            segments=[("seg", 0.0, 270.0, "Speaker 0")],
            words=[
                (
                    "world",
                    248.0,
                    258.0,
                ),  # midpoint 253 < 255 → kept, end clipped to 255
            ],
        )
        r2 = _make_result(
            segments=[("seg2", 0.0, 50.0, "Speaker 1")],
            words=[
                (
                    "foo",
                    10.0,
                    20.0,
                ),  # abs 250-260, midpoint 255 >= 255 → kept, start clipped to 255
                ("bar", 20.0, 40.0),  # abs 260-280
            ],
        )
        stitched = stitch_transcription_results(
            [(r1, 0.0, 270.0), (r2, 240.0, 510.0)],
            overlap_seconds=30.0,
        )
        world = next(w for w in stitched.words if w.word == "world")
        foo = next(w for w in stitched.words if w.word == "foo")
        assert world.end == pytest.approx(255.0)
        assert foo.start == pytest.approx(255.0)

    def test_clip_helpers_drop_degenerate(self):
        """Segments/words that become start >= end after clipping are dropped."""

        segs = [
            Segment(text="survives", start=10.0, end=20.0),
            Segment(
                text="clipped away", start=10.0, end=15.0
            ),  # start becomes 15, end=15 → dropped
            Segment(
                text="also gone", start=10.0, end=12.0
            ),  # start becomes 15 > end 12 → dropped
        ]
        result = _clip_segments(segs, left_bound=15.0, right_bound=None)
        assert len(result) == 1
        assert result[0].text == "survives"
        assert result[0].start == 15.0

        words = [
            Word(word="ok", start=5.0, end=20.0),
            Word(
                word="gone", start=5.0, end=9.0
            ),  # end becomes 9, but start=5 < 9 → survives... wait
        ]
        result = _clip_words(words, left_bound=None, right_bound=10.0)
        assert len(result) == 2  # both survive with right_bound=10
        result = _clip_words(words, left_bound=10.0, right_bound=None)
        assert len(result) == 1  # "gone" has end=9 < left_bound=10 → dropped
        assert result[0].word == "ok"


class TestSpeakerMerging:
    """Test that speaker info is properly merged across batches."""

    def test_speakers_merged(self):
        r1 = TranscriptionResult(
            text="hello",
            segments=[Segment(text="hello", start=0.0, end=5.0, speaker="Speaker 0")],
            speakers=[Speaker(id="Speaker 0", start=0.0, end=5.0)],
        )
        r2 = TranscriptionResult(
            text="world",
            segments=[Segment(text="world", start=0.0, end=5.0, speaker="Speaker 0")],
            speakers=[Speaker(id="Speaker 0", start=0.0, end=5.0)],
        )

        stitched = stitch_transcription_results(
            [(r1, 0.0, 5.0), (r2, 5.0, 10.0)],
            overlap_seconds=0,
        )

        assert stitched.speakers is not None
        assert len(stitched.speakers) == 1
        assert stitched.speakers[0].id == "Speaker 0"
        assert stitched.speakers[0].start == 0.0
        assert stitched.speakers[0].end == 10.0


# ---------------------------------------------------------------------------
# GPU integration test (requires model + GPU)
# ---------------------------------------------------------------------------

gpu_tests = pytest.mark.skipif(
    not os.getenv("RUN_GPU_TESTS"), reason="GPU tests disabled (set RUN_GPU_TESTS=1)"
)


@gpu_tests
class TestBatchedTranscriptionQuality:
    """
    Compare batched transcription against direct single-shot transcription.

    Uses the existing 4-minute test WAV. Transcribes it directly, then
    batches with small windows and compares the first 2 minutes.
    """

    _DEFAULT_AUDIO = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "tests"
        / "test_assets"
        / "DIY_Experts_Glass_Blowing_16khz_mono_4min.wav"
    )
    TEST_AUDIO = os.getenv("TEST_AUDIO_FILE") or str(_DEFAULT_AUDIO)

    @pytest.fixture(scope="class")
    def transcriber(self):
        """Load VibeVoice model once for all tests in this class."""
        # Imported here so the module does not require torch: only this GPU-only
        # class needs the VibeVoice provider.
        from providers.vibevoice.transcriber import VibeVoiceTranscriber

        t = VibeVoiceTranscriber()
        t.load_model()
        return t

    @pytest.fixture(scope="class")
    def direct_result(self, transcriber):
        """Transcribe the full file in one shot (baseline)."""
        return transcriber._transcribe_single(self.TEST_AUDIO)

    def test_direct_transcription_has_segments(self, direct_result):
        """Sanity check: direct transcription should produce segments."""
        assert len(direct_result.segments) > 0
        assert len(direct_result.text) > 0

    def test_batched_matches_direct(self, transcriber, direct_result):
        """Batched transcription of first 2 min should match direct transcription."""
        # Extract first 2 min segments as reference
        reference_segments = [s for s in direct_result.segments if s.start < 120.0]
        reference_text = " ".join(s.text for s in reference_segments)

        # Batched: use small windows (60s batch, 15s overlap) to force multiple batches
        windows = split_audio_file(self.TEST_AUDIO, batch_duration=60, overlap=15)
        batch_results = []
        prev_context = None
        for temp_path, start, end in windows:
            try:
                result = transcriber._transcribe_single(temp_path, context=prev_context)
                batch_results.append((result, start, end))
                prev_context = extract_context_tail(result)
            finally:
                os.unlink(temp_path)

        stitched = stitch_transcription_results(batch_results, overlap_seconds=15)

        # Extract first 2 min from stitched
        stitched_first_2min = [s for s in stitched.segments if s.start < 120.0]
        stitched_text = " ".join(s.text for s in stitched_first_2min)

        # Compare
        similarity = difflib.SequenceMatcher(
            None, reference_text, stitched_text
        ).ratio()

        assert (
            len(stitched_first_2min) >= len(reference_segments) - 2
        ), f"Batched has too few segments: {len(stitched_first_2min)} vs {len(reference_segments)}"
        assert similarity > 0.7, f"Text similarity too low: {similarity:.2f}"

        # Verify no timestamp gaps > 5s in stitched output
        for i in range(1, len(stitched_first_2min)):
            gap = stitched_first_2min[i].start - stitched_first_2min[i - 1].end
            assert gap < 5.0, f"Gap of {gap:.1f}s between segments {i-1} and {i}"

    def test_batched_covers_full_duration(self, transcriber):
        """Batched transcription should cover the full audio duration."""
        windows = split_audio_file(self.TEST_AUDIO, batch_duration=60, overlap=15)
        batch_results = []
        prev_context = None
        for temp_path, start, end in windows:
            try:
                result = transcriber._transcribe_single(temp_path, context=prev_context)
                batch_results.append((result, start, end))
                prev_context = extract_context_tail(result)
            finally:
                os.unlink(temp_path)

        stitched = stitch_transcription_results(batch_results, overlap_seconds=15)

        # Should cover most of the ~4 minute audio
        assert stitched.duration is not None
        assert (
            stitched.duration > 200.0
        ), f"Stitched duration {stitched.duration:.1f}s seems too short for ~4min audio"

    def test_batched_segments_clipped_at_boundaries(self, transcriber):
        """No segment should extend past its batch's cutoff boundary.

        Within-batch overlaps (e.g. Music + Speech) are legitimate, so we
        check boundary clipping rather than pairwise non-overlap.
        """
        overlap_secs = 15
        windows = split_audio_file(
            self.TEST_AUDIO, batch_duration=60, overlap=overlap_secs
        )

        # Compute cutoffs between consecutive batches
        cutoffs = []
        for j in range(len(windows) - 1):
            _, next_start, _ = windows[j + 1]
            cutoffs.append(next_start + overlap_secs / 2)

        batch_results = []
        for temp_path, start, end in windows:
            try:
                result = transcriber._transcribe_single(temp_path)
                batch_results.append((result, start, end))
            finally:
                os.unlink(temp_path)

        stitched = stitch_transcription_results(
            batch_results, overlap_seconds=overlap_secs
        )

        # Every segment's end should respect the next cutoff boundary
        for seg in stitched.segments:
            for c in cutoffs:
                mid = (seg.start + seg.end) / 2
                if mid < c:
                    # Segment assigned to batch before this cutoff
                    assert seg.end <= c + 0.01, (
                        f"Segment [{seg.start:.1f}-{seg.end:.1f}] '{seg.text[:30]}' "
                        f"bleeds past cutoff {c:.1f}"
                    )
                    break


# ---------------------------------------------------------------------------
# Ground truth fixture test (no GPU needed, requires fixture file)
# ---------------------------------------------------------------------------


class TestGroundTruthFixture:
    """
    Validate VibeVoice output against a reviewed ground truth fixture.

    Run capture_vibevoice_ground_truth.py to generate the fixture, then
    review and commit it. These tests validate structural properties of
    the output (no overlaps, valid timestamps, etc.).
    """

    FIXTURE_PATH = (
        Path(__file__).parent / "fixtures" / "vibevoice_4min_ground_truth.json"
    )

    @pytest.fixture
    def ground_truth(self):
        if not self.FIXTURE_PATH.exists():
            pytest.skip(
                f"Ground truth fixture not found: {self.FIXTURE_PATH}\n"
                f"Run: uv run python tests/capture_vibevoice_ground_truth.py"
            )
        with open(self.FIXTURE_PATH) as f:
            return json.load(f)

    def test_segments_no_overlaps(self, ground_truth):
        """Ground truth segments must not overlap."""
        segments = ground_truth.get("segments", [])
        assert len(segments) > 0, "No segments in ground truth"

        for i in range(len(segments) - 1):
            overlap = segments[i]["end"] - segments[i + 1]["start"]
            assert overlap <= 0.01, (
                f"Segments {i} and {i+1} overlap by {overlap:.2f}s: "
                f"[{segments[i]['start']:.1f}-{segments[i]['end']:.1f}] "
                f"'{segments[i]['text'][:30]}' vs "
                f"[{segments[i+1]['start']:.1f}-{segments[i+1]['end']:.1f}] "
                f"'{segments[i+1]['text'][:30]}'"
            )

    def test_segments_ordered(self, ground_truth):
        """Segments must be in chronological order."""
        segments = ground_truth.get("segments", [])
        for i in range(len(segments) - 1):
            assert segments[i]["start"] <= segments[i + 1]["start"], (
                f"Segment {i} starts after segment {i+1}: "
                f"{segments[i]['start']:.1f} > {segments[i+1]['start']:.1f}"
            )

    def test_segments_have_valid_timestamps(self, ground_truth):
        """All segments must have start < end."""
        for i, seg in enumerate(ground_truth.get("segments", [])):
            assert (
                seg["start"] < seg["end"]
            ), f"Segment {i} has invalid timing: start={seg['start']}, end={seg['end']}"

    def test_segments_have_text(self, ground_truth):
        """All segments must have non-empty text."""
        for i, seg in enumerate(ground_truth.get("segments", [])):
            assert seg.get("text", "").strip(), f"Segment {i} has empty text"

    def test_has_speaker_labels(self, ground_truth):
        """At least some segments should have speaker labels."""
        segments = ground_truth.get("segments", [])
        with_speaker = [s for s in segments if s.get("speaker")]
        assert len(with_speaker) > 0, "No segments have speaker labels"
