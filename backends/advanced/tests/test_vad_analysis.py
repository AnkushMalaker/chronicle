"""Unit tests for VAD gap detection and speech-fraction derivation."""

from advanced_omi_backend.utils.vad_analysis import (
    HISTOGRAM_BIN_WIDTH,
    detect_silence_gaps,
    speech_fraction_from_histogram,
)


class TestDetectSilenceGaps:
    @staticmethod
    def chunk(index, start, end, score):
        return {
            "chunk_index": index,
            "start_time": start,
            "end_time": end,
            "vad": {"max_score": score},
        }

    def test_single_gap(self):
        chunks = [
            self.chunk(0, 0.0, 20.0, 0.9),
            self.chunk(1, 20.0, 1020.0, 0.1),
            self.chunk(2, 1020.0, 1040.0, 0.9),
        ]
        gaps = detect_silence_gaps(chunks, min_gap_seconds=900)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap["start_seconds"] == 20.0
        assert gap["end_seconds"] == 1020.0
        assert gap["duration_seconds"] == 1000.0
        # split where speech resumes
        assert gap["split_point_seconds"] == 1020.0

    def test_multiple_gaps(self):
        chunks = [
            self.chunk(0, 0.0, 10.0, 0.9),
            self.chunk(1, 10.0, 960.0, 0.1),
            self.chunk(2, 960.0, 970.0, 0.9),
            self.chunk(3, 970.0, 1920.0, 0.1),
            self.chunk(4, 1920.0, 1930.0, 0.9),
        ]
        gaps = detect_silence_gaps(chunks, min_gap_seconds=900)
        assert len(gaps) == 2
        assert gaps[0]["start_seconds"] == 10.0
        assert gaps[1]["end_seconds"] == 1920.0

    def test_gap_shorter_than_min_excluded(self):
        chunks = [
            self.chunk(0, 0.0, 10.0, 0.9),
            self.chunk(1, 10.0, 810.0, 0.1),
            self.chunk(2, 810.0, 820.0, 0.9),
        ]
        assert detect_silence_gaps(chunks, min_gap_seconds=900) == []

    def test_leading_and_trailing_excluded(self):
        leading = [
            self.chunk(0, 0.0, 1000.0, 0.1),
            self.chunk(1, 1000.0, 1010.0, 0.9),
        ]
        trailing = [
            self.chunk(0, 0.0, 10.0, 0.9),
            self.chunk(1, 10.0, 1010.0, 0.1),
        ]
        assert detect_silence_gaps(leading, min_gap_seconds=900) == []
        assert detect_silence_gaps(trailing, min_gap_seconds=900) == []

    def test_empty(self):
        assert detect_silence_gaps([], min_gap_seconds=900) == []

    def test_empty_input(self):
        assert detect_silence_gaps([]) == []


class TestSpeechFraction:
    def test_all_silent(self):
        histogram = [100] + [0] * 19  # all frames in the lowest prob bin
        assert (
            speech_fraction_from_histogram(histogram, 100, HISTOGRAM_BIN_WIDTH, 0.5)
            == 0.0
        )

    def test_all_speech(self):
        histogram = [0] * 19 + [100]
        assert (
            speech_fraction_from_histogram(histogram, 100, HISTOGRAM_BIN_WIDTH, 0.5)
            == 1.0
        )

    def test_half_speech(self):
        histogram = [50] + [0] * 18 + [50]
        assert (
            speech_fraction_from_histogram(histogram, 100, HISTOGRAM_BIN_WIDTH, 0.5)
            == 0.5
        )

    def test_threshold_moves_boundary(self):
        # 100 frames in bin [0.45, 0.5) — below 0.5, above 0.4
        histogram = [0] * 9 + [100] + [0] * 10
        assert (
            speech_fraction_from_histogram(histogram, 100, HISTOGRAM_BIN_WIDTH, 0.5)
            == 0.0
        )
        assert (
            speech_fraction_from_histogram(histogram, 100, HISTOGRAM_BIN_WIDTH, 0.45)
            == 1.0
        )

    def test_empty(self):
        assert speech_fraction_from_histogram([], 0, HISTOGRAM_BIN_WIDTH, 0.5) == 0.0


class TestSpeechRegions:
    def test_frame_intervals_basic(self):
        from advanced_omi_backend.utils.vad_analysis import frame_speech_intervals

        # 10 frames at 0.1s hop, speech in frames 2-4 and 7-9, offset 100s
        scores = [0.1, 0.1, 0.9, 0.9, 0.9, 0.1, 0.1, 0.8, 0.8, 0.8]
        intervals = frame_speech_intervals(scores, 0.1, 100.0)
        assert intervals == [[100.2, 100.5], [100.7, 101.0]]

    def test_frame_intervals_run_to_end(self):
        from advanced_omi_backend.utils.vad_analysis import frame_speech_intervals

        intervals = frame_speech_intervals([0.9, 0.9], 0.5, 0.0)
        assert intervals == [[0.0, 1.0]]

    def test_merge_pads_and_merges(self):
        from advanced_omi_backend.utils.vad_analysis import merge_speech_regions

        # Two 1s intervals 2s apart → padded by 0.3 → gap 1.4s < 3s → one region
        regions = merge_speech_regions([[10.0, 11.0], [13.0, 14.0]], 100.0)
        assert regions == [[9.7, 14.3]]

    def test_merge_keeps_distant_regions_separate(self):
        from advanced_omi_backend.utils.vad_analysis import merge_speech_regions

        regions = merge_speech_regions([[10.0, 11.0], [30.0, 31.0]], 100.0)
        assert len(regions) == 2

    def test_merge_drops_blips(self):
        from advanced_omi_backend.utils.vad_analysis import merge_speech_regions

        # 0.2s blip < 0.4s min → dropped
        assert merge_speech_regions([[10.0, 10.2]], 100.0) == []

    def test_merge_clamps_to_bounds(self):
        from advanced_omi_backend.utils.vad_analysis import merge_speech_regions

        regions = merge_speech_regions([[0.1, 1.0], [98.8, 99.9]], 100.0)
        assert regions[0][0] == 0.0
        assert regions[-1][1] == 100.0

    def test_merge_caps_region_count(self):
        from advanced_omi_backend.utils.vad_analysis import merge_speech_regions

        # 1000 intervals 10s apart — beyond the cap, merge gap doubles until it fits
        intervals = [[i * 10.0, i * 10.0 + 1.0] for i in range(1000)]
        regions = merge_speech_regions(intervals, 10010.0, max_count=100)
        assert len(regions) <= 100

    def test_merge_empty(self):
        from advanced_omi_backend.utils.vad_analysis import merge_speech_regions

        assert merge_speech_regions([], 100.0) == []


class TestDetectSpeechPcm:
    """Structured speech verdicts distinguish rejection from fail-open reasons."""

    class FakeProvider:
        name = "fixture-vad"
        frame_hop_ms = 16

        def __init__(self, scores):
            self._scores = scores

        def score(self, mono, sample_rate):
            return self._scores

    @staticmethod
    def pcm(seconds=1.0, sample_rate=16000):
        return b"\x00\x00" * int(seconds * sample_rate)

    def patch_provider(self, monkeypatch, scores):
        import advanced_omi_backend.utils.vad_analysis as vad_analysis

        monkeypatch.setattr(
            vad_analysis, "get_vad_provider", lambda: self.FakeProvider(scores)
        )

    def test_sustained_speech_detected(self, monkeypatch):
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        # 40 frames * 16ms = 0.64s of speech, above REGION_MIN_SECONDS
        self.patch_provider(monkeypatch, [0.9] * 40 + [0.1] * 20)
        result = detect_speech_pcm(self.pcm(), 16000, 1, 2)

        assert result.has_speech is True
        assert result.scored is True
        assert result.should_reject is False
        assert result.reason is SpeechDetectionReason.SPEECH_DETECTED

    def test_silence_rejected(self, monkeypatch):
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        self.patch_provider(monkeypatch, [0.1] * 60)
        result = detect_speech_pcm(self.pcm(), 16000, 1, 2)

        assert result.has_speech is False
        assert result.scored is True
        assert result.should_reject is True
        assert result.reason is SpeechDetectionReason.NO_SPEECH

    def test_isolated_blip_rejected(self, monkeypatch):
        from advanced_omi_backend.utils.vad_analysis import detect_speech_pcm

        # 10 frames * 16ms = 0.16s, below REGION_MIN_SECONDS (0.4s)
        self.patch_provider(monkeypatch, [0.1] * 20 + [0.9] * 10 + [0.1] * 30)
        assert detect_speech_pcm(self.pcm(), 16000, 1, 2).should_reject is True

    def test_provider_unavailable_fails_open_with_reason(self, monkeypatch):
        import advanced_omi_backend.utils.vad_analysis as vad_analysis
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        def boom():
            raise RuntimeError("no native lib")

        monkeypatch.setattr(vad_analysis, "get_vad_provider", boom)
        result = detect_speech_pcm(self.pcm(), 16000, 1, 2)

        assert result.has_speech is None
        assert result.scored is False
        assert result.should_reject is False
        assert result.reason is SpeechDetectionReason.PROVIDER_UNAVAILABLE
        assert result.detail == "RuntimeError: no native lib"

    def test_provider_scoring_failure_fails_open_with_reason(self, monkeypatch):
        import advanced_omi_backend.utils.vad_analysis as vad_analysis
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        class BrokenProvider:
            frame_hop_ms = 16

            def score(self, mono, sample_rate):
                raise RuntimeError("native process failed")

        monkeypatch.setattr(
            vad_analysis,
            "get_vad_provider",
            lambda: BrokenProvider(),
        )
        result = detect_speech_pcm(self.pcm(), 16000, 1, 2)

        assert result.has_speech is None
        assert result.reason is SpeechDetectionReason.PROVIDER_ERROR
        assert result.detail == "RuntimeError: native process failed"

    def test_unsupported_sample_width_fails_open(self):
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        result = detect_speech_pcm(self.pcm(), 16000, 1, 3)

        assert result.has_speech is None
        assert result.scored is False
        assert result.should_reject is False
        assert result.reason is SpeechDetectionReason.UNSUPPORTED_SAMPLE_WIDTH

    def test_invalid_audio_metadata_has_specific_fail_open_reasons(self):
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        invalid_channels = detect_speech_pcm(self.pcm(), 16000, 0, 2)
        invalid_sample_rate = detect_speech_pcm(self.pcm(), 0, 1, 2)

        assert invalid_channels.has_speech is None
        assert invalid_channels.reason is SpeechDetectionReason.INVALID_CHANNELS
        assert invalid_sample_rate.has_speech is None
        assert invalid_sample_rate.reason is SpeechDetectionReason.INVALID_SAMPLE_RATE

    def test_fail_open_log_contains_stable_reason(self, caplog):
        from advanced_omi_backend.utils.vad_analysis import detect_speech_pcm

        detect_speech_pcm(self.pcm(), 16000, 1, 3)

        assert "speech_gate_unscored reason=unsupported_sample_width" in caplog.text

    def test_empty_audio_is_not_speech(self):
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        result = detect_speech_pcm(b"", 16000, 1, 2)

        assert result.has_speech is False
        assert result.scored is False
        assert result.should_reject is True
        assert result.reason is SpeechDetectionReason.EMPTY_AUDIO

    def test_malformed_pcm_fails_open_with_preparation_reason(self):
        from advanced_omi_backend.utils.vad_analysis import (
            SpeechDetectionReason,
            detect_speech_pcm,
        )

        result = detect_speech_pcm(b"\x00\x00\x00", 16000, 1, 2)

        assert result.has_speech is None
        assert result.should_reject is False
        assert result.reason is SpeechDetectionReason.PCM_PREPARATION_FAILED
