"""
Test suite for conversation models.

Tests that don't need Beanie initialization (pure model validation).
"""

from datetime import datetime

from advanced_omi_backend.models.conversation import Conversation


class TestConversationModel:
    """Test Conversation Pydantic model (no DB required)."""

    def test_speaker_segment_model(self):
        """Test SpeakerSegment model."""
        segment = Conversation.SpeakerSegment(
            start=10.5,
            end=15.8,
            text="Hello, how are you today?",
            speaker="Speaker A",
            confidence=0.95,
        )

        assert segment.start == 10.5
        assert segment.end == 15.8
        assert segment.text == "Hello, how are you today?"
        assert segment.speaker == "Speaker A"
        assert segment.confidence == 0.95

    def test_transcript_version_model(self):
        """Test TranscriptVersion model."""
        segments = [
            Conversation.SpeakerSegment(
                start=0.0, end=5.0, text="Hello", speaker="Speaker A"
            ),
            Conversation.SpeakerSegment(
                start=5.1, end=10.0, text="Hi there", speaker="Speaker B"
            ),
        ]

        version = Conversation.TranscriptVersion(
            version_id="trans-v1",
            transcript="Hello Hi there",
            segments=segments,
            provider="deepgram",
            model="nova-3",
            created_at=datetime.now(),
            processing_time_seconds=12.5,
            metadata={"confidence": 0.9},
        )

        assert version.version_id == "trans-v1"
        assert version.transcript == "Hello Hi there"
        assert len(version.segments) == 2
        assert version.provider == "deepgram"
        assert version.model == "nova-3"
        assert version.processing_time_seconds == 12.5
        assert version.metadata["confidence"] == 0.9

    def test_memory_version_model(self):
        """Test MemoryVersion model."""
        version = Conversation.MemoryVersion(
            version_id="mem-v1",
            memory_count=5,
            transcript_version_id="trans-v1",
            provider=Conversation.MemoryProvider.CHRONICLE,
            model="gpt-4o-mini",
            created_at=datetime.now(),
            processing_time_seconds=45.2,
            metadata={"extraction_quality": "high"},
        )

        assert version.version_id == "mem-v1"
        assert version.memory_count == 5
        assert version.transcript_version_id == "trans-v1"
        assert version.provider == Conversation.MemoryProvider.CHRONICLE
        assert version.model == "gpt-4o-mini"
        assert version.processing_time_seconds == 45.2
        assert version.metadata["extraction_quality"] == "high"

    def test_provider_enums(self):
        """Test that provider enums work correctly."""
        assert Conversation.MemoryProvider.CHRONICLE == "chronicle"
        assert Conversation.MemoryProvider.OPENMEMORY_MCP == "openmemory_mcp"

    def test_word_model(self):
        """Test Word model."""
        word = Conversation.Word(word="hello", start=0.0, end=0.5, confidence=0.98)
        assert word.word == "hello"
        assert word.start == 0.0
        assert word.end == 0.5
        assert word.confidence == 0.98

    def test_speaker_segment_defaults(self):
        """Test SpeakerSegment default values."""
        segment = Conversation.SpeakerSegment(
            start=0.0, end=1.0, text="Test", speaker="Speaker 0"
        )
        assert segment.confidence is None
        assert segment.identified_as is None
        assert segment.words == []

    def test_transcript_version_defaults(self):
        """Test TranscriptVersion default values."""
        version = Conversation.TranscriptVersion(
            version_id="v1",
            created_at=datetime.now(),
        )
        assert version.transcript is None
        assert version.words == []
        assert version.segments == []
        assert version.provider is None
        assert version.model is None
        assert version.processing_time_seconds is None
        assert version.metadata == {}


class TestAnnotationApplyMetadataPropagation:
    """Test that annotation apply correctly propagates provider metadata.

    The annotation apply endpoints create new transcript versions. They must
    carry over provider_capabilities and diarization_source from the source
    version so downstream processing knows provider capabilities.

    Uses TranscriptVersion directly (no DB) since the propagation logic
    operates on version objects, not the Conversation document.
    """

    def _make_source_version(
        self,
        provider_capabilities=None,
        diarization_source=None,
    ):
        """Helper: create a source TranscriptVersion."""
        metadata = {"some_key": "some_value"}
        if provider_capabilities is not None:
            metadata["provider_capabilities"] = provider_capabilities

        version = Conversation.TranscriptVersion(
            version_id="v1",
            transcript="Hello world",
            segments=[
                Conversation.SpeakerSegment(
                    start=0.0, end=5.0, text="Hello world", speaker="Speaker 0"
                )
            ],
            provider="vibevoice",
            model="microsoft/VibeVoice-ASR-HF",
            created_at=datetime.now(),
            metadata=metadata,
            diarization_source=diarization_source,
        )
        return version

    def _simulate_annotation_apply(self, source):
        """Reproduce the exact metadata propagation logic from annotation routes."""
        source_capabilities = source.metadata.get("provider_capabilities", {})
        new_version = Conversation.TranscriptVersion(
            version_id="v2",
            transcript=source.transcript,
            segments=[s.model_copy() for s in source.segments],
            provider=source.provider,
            model=source.model,
            created_at=datetime.now(),
            metadata={
                "reprocessing_type": "diarization_annotations",
                "source_version_id": source.version_id,
                "provider_capabilities": source_capabilities,
            },
        )
        if source.diarization_source:
            new_version.diarization_source = source.diarization_source
        return new_version

    def test_capabilities_and_diarization_source_propagated(self):
        """Both provider_capabilities and diarization_source carry through."""
        source = self._make_source_version(
            provider_capabilities={"segments": True, "diarization": True},
            diarization_source="provider",
        )
        new_version = self._simulate_annotation_apply(source)

        assert new_version.metadata["provider_capabilities"] == {
            "segments": True,
            "diarization": True,
        }
        assert new_version.diarization_source == "provider"

    def test_no_provider_capabilities_in_source(self):
        """Source version has no provider_capabilities → new version gets {}."""
        source = self._make_source_version(
            provider_capabilities=None,  # not in metadata at all
        )
        new_version = self._simulate_annotation_apply(source)

        assert new_version.metadata["provider_capabilities"] == {}
        assert new_version.diarization_source is None

    def test_empty_provider_capabilities(self):
        """Source version has empty provider_capabilities → preserved as {}."""
        source = self._make_source_version(provider_capabilities={})
        new_version = self._simulate_annotation_apply(source)

        assert new_version.metadata["provider_capabilities"] == {}

    def test_diarization_source_none_not_propagated(self):
        """Source with diarization_source=None → new version stays None."""
        source = self._make_source_version(
            provider_capabilities={"segments": True},
            diarization_source=None,
        )
        new_version = self._simulate_annotation_apply(source)

        assert new_version.diarization_source is None

    def test_pyannote_diarization_source_propagated(self):
        """diarization_source='pyannote' carries through correctly."""
        source = self._make_source_version(
            provider_capabilities={"segments": True, "word_timestamps": True},
            diarization_source="pyannote",
        )
        new_version = self._simulate_annotation_apply(source)

        assert new_version.diarization_source == "pyannote"

    def test_chained_applies_preserve_metadata(self):
        """Apply v1→v2, then v2→v3: capabilities survive both hops."""
        v1 = self._make_source_version(
            provider_capabilities={"segments": True, "diarization": True},
            diarization_source="provider",
        )
        v2 = self._simulate_annotation_apply(v1)

        # Second apply: v2 → v3
        source_capabilities = v2.metadata.get("provider_capabilities", {})
        v3 = Conversation.TranscriptVersion(
            version_id="v3",
            transcript=v2.transcript,
            segments=[s.model_copy() for s in v2.segments],
            provider=v2.provider,
            model=v2.model,
            created_at=datetime.now(),
            metadata={
                "reprocessing_type": "unified_annotations",
                "source_version_id": v2.version_id,
                "provider_capabilities": source_capabilities,
            },
        )
        if v2.diarization_source:
            v3.diarization_source = v2.diarization_source

        assert v3.metadata["provider_capabilities"] == {
            "segments": True,
            "diarization": True,
        }
        assert v3.diarization_source == "provider"

    def test_segments_copied_not_shared(self):
        """Apply creates independent segment copies (model_copy)."""
        source = self._make_source_version(
            provider_capabilities={"segments": True},
            diarization_source="provider",
        )
        new_version = self._simulate_annotation_apply(source)

        # Mutating new version's segment shouldn't affect source
        new_version.segments[0].speaker = "CHANGED"
        assert source.segments[0].speaker == "Speaker 0"

    def test_diarization_correction_applied_to_segments(self):
        """Simulate the actual diarization apply: correct speaker on segment copy."""
        source = self._make_source_version(
            provider_capabilities={"segments": True, "diarization": True},
            diarization_source="provider",
        )
        # Simulate annotation apply with speaker correction
        corrected_segments = []
        for seg in source.segments:
            corrected = seg.model_copy()
            corrected.speaker = "Alice"  # Correction from annotation
            corrected_segments.append(corrected)

        source_capabilities = source.metadata.get("provider_capabilities", {})
        new_version = Conversation.TranscriptVersion(
            version_id="v2",
            transcript=source.transcript,
            segments=corrected_segments,
            words=source.words,
            provider=source.provider,
            model=source.model,
            created_at=datetime.now(),
            metadata={
                "reprocessing_type": "diarization_annotations",
                "source_version_id": source.version_id,
                "provider_capabilities": source_capabilities,
            },
        )
        if source.diarization_source:
            new_version.diarization_source = source.diarization_source

        # Speaker corrected
        assert new_version.segments[0].speaker == "Alice"
        # Source unchanged
        assert source.segments[0].speaker == "Speaker 0"
        # Metadata propagated
        assert new_version.metadata["provider_capabilities"] == {
            "segments": True,
            "diarization": True,
        }
        assert new_version.diarization_source == "provider"


class TestAnnotationApplyTimingPreservation:
    """Test that annotation apply never corrupts segment start/end timing.

    The apply endpoints use model_copy() and only mutate speaker/text.
    Segment timestamps must pass through unchanged.
    """

    def _make_segments(self):
        """Multi-segment source with realistic timing."""
        return [
            Conversation.SpeakerSegment(
                start=0.0, end=5.5, text="Hello everyone", speaker="Speaker 0"
            ),
            Conversation.SpeakerSegment(
                start=5.5, end=12.3, text="Welcome to the show", speaker="Speaker 1"
            ),
            Conversation.SpeakerSegment(
                start=12.3, end=18.9, text="Thanks for having me", speaker="Speaker 0"
            ),
        ]

    def test_diarization_apply_preserves_timing(self):
        """Diarization corrections only touch speaker, timing unchanged."""
        segments = self._make_segments()
        original_times = [(s.start, s.end) for s in segments]

        corrected = []
        for seg in segments:
            c = seg.model_copy()
            c.speaker = "Alice"
            corrected.append(c)

        for i, seg in enumerate(corrected):
            assert seg.start == original_times[i][0]
            assert seg.end == original_times[i][1]

    def test_transcript_apply_preserves_timing(self):
        """Transcript text corrections only touch text, timing unchanged."""
        segments = self._make_segments()
        original_times = [(s.start, s.end) for s in segments]

        corrected = []
        for seg in segments:
            c = seg.model_copy()
            c.text = "CORRECTED TEXT"
            corrected.append(c)

        for i, seg in enumerate(corrected):
            assert seg.start == original_times[i][0]
            assert seg.end == original_times[i][1]

    def test_combined_diarization_and_text_preserves_timing(self):
        """Both speaker + text changes applied, timing still unchanged."""
        segments = self._make_segments()
        original_times = [(s.start, s.end) for s in segments]

        corrected = []
        for seg in segments:
            c = seg.model_copy()
            c.speaker = "Bob"
            c.text = "Different text"
            corrected.append(c)

        for i, seg in enumerate(corrected):
            assert seg.start == original_times[i][0]
            assert seg.end == original_times[i][1]

    def test_unannotated_segments_pass_through_unchanged(self):
        """Segments without annotations keep all fields identical."""
        segments = self._make_segments()

        corrected = [seg.model_copy() for seg in segments]

        for orig, copy in zip(segments, corrected):
            assert copy.start == orig.start
            assert copy.end == orig.end
            assert copy.text == orig.text
            assert copy.speaker == orig.speaker

    def test_insert_annotation_creates_zero_duration_segment(self):
        """Insert annotations create zero-duration event markers, not real segments."""
        segments = self._make_segments()

        # Simulate insert after segment 1 (index 1)
        insert_pos = 2  # after index 1
        boundary_time = segments[insert_pos - 1].end  # 12.3

        inserted = Conversation.SpeakerSegment(
            start=boundary_time,
            end=boundary_time,
            text="[applause]",
            speaker="",
            segment_type="event",
        )

        result = list(segments)
        result.insert(insert_pos, inserted)

        # Original segments untouched
        assert result[0].start == 0.0
        assert result[0].end == 5.5
        assert result[1].start == 5.5
        assert result[1].end == 12.3
        # Inserted segment is zero-duration at boundary
        assert result[2].start == 12.3
        assert result[2].end == 12.3
        assert result[2].text == "[applause]"
        # Following segment timing unchanged
        assert result[3].start == 12.3
        assert result[3].end == 18.9

    def test_partial_annotation_only_affects_targeted_segments(self):
        """When only segment 1 has annotation, segments 0 and 2 are untouched."""
        segments = self._make_segments()
        original_times = [(s.start, s.end, s.text, s.speaker) for s in segments]

        corrected = []
        for i, seg in enumerate(segments):
            c = seg.model_copy()
            if i == 1:
                c.speaker = "Bob"
            corrected.append(c)

        # Segment 0: fully unchanged
        assert corrected[0].start == original_times[0][0]
        assert corrected[0].end == original_times[0][1]
        assert corrected[0].text == original_times[0][2]
        assert corrected[0].speaker == original_times[0][3]
        # Segment 1: only speaker changed, timing preserved
        assert corrected[1].start == original_times[1][0]
        assert corrected[1].end == original_times[1][1]
        assert corrected[1].text == original_times[1][2]
        assert corrected[1].speaker == "Bob"
        # Segment 2: fully unchanged
        assert corrected[2].start == original_times[2][0]
        assert corrected[2].end == original_times[2][1]
        assert corrected[2].text == original_times[2][2]
        assert corrected[2].speaker == original_times[2][3]
