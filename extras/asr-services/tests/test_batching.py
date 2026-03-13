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
