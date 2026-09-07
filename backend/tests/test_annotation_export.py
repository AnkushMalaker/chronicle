"""Unit tests for annotation-export helpers (utils/annotation_export.py)."""

from backend.models.conversation import Conversation
from backend.utils.annotation_export import (
    build_clip_record,
    new_export_id,
    validate_export_id,
)
from backend.utils.transcript_slicing import slice_segments


def _segment(start: float, end: float, text: str, speaker: str = "speaker_0"):
    return Conversation.SpeakerSegment(start=start, end=end, text=text, speaker=speaker)


class TestExportId:
    def test_new_export_id_is_valid(self):
        export_id = new_export_id()
        assert validate_export_id(export_id)

    def test_rejects_path_traversal(self):
        assert not validate_export_id("../etc")
        assert not validate_export_id("annotation_20260611_120000_ab12/../x")
        assert not validate_export_id("annotation_20260611_120000_AB12")  # uppercase
        assert not validate_export_id("other_20260611_120000_ab12")

    def test_ids_are_unique(self):
        assert new_export_id() != new_export_id()


class TestBuildClipRecord:
    def _record(self, segments, t0=100.0, t1=130.0, idx=2):
        return build_clip_record(
            conversation_id="conv-abc",
            conversation_title="Morning chat",
            client_id="user01-phone",
            conversation_created_at="2026-06-11T08:00:00+00:00",
            clip_index=idx,
            region_start=t0,
            region_end=t1,
            sample_rate=16000,
            segments=slice_segments(segments, t0, t1),
        )

    def test_basic_record(self):
        segments = [
            _segment(90.0, 99.0, "before the clip"),
            _segment(101.0, 105.0, "hello there"),
            _segment(110.0, 120.0, "how are you", speaker="speaker_1"),
            _segment(140.0, 150.0, "after the clip"),
        ]
        rec = self._record(segments)

        assert rec["clip_id"] == "conv-abc_002"
        assert rec["audio_path"] == "audio/conv-abc_002.wav"
        assert rec["source_start_seconds"] == 100.0
        assert rec["source_end_seconds"] == 130.0
        assert rec["duration_seconds"] == 30.0
        # Only the two in-clip segments survive, shifted to clip-relative time.
        assert [s["text"] for s in rec["segments"]] == ["hello there", "how are you"]
        assert rec["segments"][0]["start"] == 1.0
        assert rec["segments"][0]["end"] == 5.0
        assert rec["segments"][1]["speaker"] == "speaker_1"
        assert rec["text"] == "hello there how are you"
        # Annotation block present and empty (the annotator's contract).
        assert rec["annotation"] == {"text": None, "segments": None, "notes": None}

    def test_clip_without_transcript(self):
        rec = self._record([_segment(0.0, 5.0, "far away")])
        assert rec["text"] == ""
        assert rec["segments"] == []

    def test_segment_straddling_clip_start_is_clamped(self):
        # Midpoint (101.5) inside [100, 130) → kept, clamped to clip start.
        rec = self._record([_segment(98.0, 105.0, "straddler")])
        assert len(rec["segments"]) == 1
        assert rec["segments"][0]["start"] == 0.0
        assert rec["segments"][0]["end"] == 5.0
