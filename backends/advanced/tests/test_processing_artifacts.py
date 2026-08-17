"""Tests for absolute-time processing evidence."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from advanced_omi_backend.models.audio_capture import AudioRangeRef
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.services import processing_artifacts
from advanced_omi_backend.services.audio_claims import map_presentation_interval
from advanced_omi_backend.services.processing_artifacts import absolute_time_for_offset


def _range(source: str, chunk: str, start: datetime, duration: float) -> AudioRangeRef:
    return AudioRangeRef(
        capture_source_id=source,
        time_basis="recorded",
        chunk_ids=[chunk],
        started_at=start,
        ended_at=start + timedelta(seconds=duration),
    )


def test_presentation_offsets_map_across_discontinuous_capture_ranges():
    first_start = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    second_start = first_start + timedelta(minutes=10)
    ranges = [
        _range("wearable", "64b64b64b64b64b64b64b641", first_start, 30),
        _range("wearable", "64b64b64b64b64b64b64b642", second_start, 20),
    ]

    assert absolute_time_for_offset(ranges, 12) == first_start + timedelta(seconds=12)
    assert absolute_time_for_offset(ranges, 35) == second_start + timedelta(seconds=5)


def test_range_boundary_can_mean_previous_end_or_next_start():
    first_start = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    second_start = first_start + timedelta(minutes=10)
    ranges = [
        _range("wearable", "64b64b64b64b64b64b64b641", first_start, 30),
        _range("wearable", "64b64b64b64b64b64b64b642", second_start, 20),
    ]

    assert absolute_time_for_offset(ranges, 30) == second_start
    assert absolute_time_for_offset(
        ranges, 30, prefer_previous_boundary=True
    ) == first_start + timedelta(seconds=30)


@pytest.mark.parametrize("offset", [-0.1, 50.1])
def test_offsets_outside_the_claim_are_rejected(offset: float):
    start = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    ranges = [_range("wearable", "64b64b64b64b64b64b64b641", start, 50)]

    with pytest.raises(ValueError, match="outside audio duration"):
        absolute_time_for_offset(ranges, offset)


@pytest.mark.asyncio
async def test_diarization_artifact_persists_from_capture_claim_without_conversation(
    monkeypatch,
):
    start = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    audio_ranges = [_range("wearable", "64b64b64b64b64b64b64b641", start, 30)]

    class QueryField:
        def __eq__(self, value):
            return ("retry_key", value)

    class StoredArtifact:
        retry_key = QueryField()
        find_one = AsyncMock(return_value=None)

        def __init__(self, **values):
            self.__dict__.update(values)
            self.artifact_id = "artifact-1"

        async def insert(self):
            await insert(self)

    insert = AsyncMock()
    monkeypatch.setattr(processing_artifacts, "DiarizationArtifact", StoredArtifact)

    artifact = await processing_artifacts.persist_diarization_artifact(
        user_id="user-1",
        audio_ranges=audio_ranges,
        retry_key="pyannote:capture-1:0",
        provider="pyannote",
        model="community-1",
        segments=[{"start": 2.0, "end": 8.0, "speaker": "SPEAKER_00"}],
        configuration={"window_seconds": 1200},
    )

    assert artifact.user_id == "user-1"
    assert artifact.audio_ranges == audio_ranges
    assert artifact.turns[0].start_seconds == 2
    assert artifact.turns[0].end_seconds == 8
    assert artifact.turns[0].audio_spans[0].started_at == start + timedelta(seconds=2)
    assert artifact.turns[0].audio_spans[0].ended_at == start + timedelta(seconds=8)
    insert.assert_awaited_once_with(artifact)


def test_presentation_interval_splits_across_overlapping_wall_clock_ranges():
    start = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    ranges = [
        _range("wearable", "64b64b64b64b64b64b64b641", start, 10),
        _range(
            "wearable",
            "64b64b64b64b64b64b64b642",
            start + timedelta(seconds=9),
            10,
        ),
    ]

    spans = map_presentation_interval(ranges, 9.5, 10.5)

    assert [
        (span.audio_range_id, span.started_at, span.ended_at) for span in spans
    ] == [
        (
            ranges[0].range_id,
            start + timedelta(seconds=9.5),
            start + timedelta(seconds=10),
        ),
        (
            ranges[1].range_id,
            start + timedelta(seconds=9),
            start + timedelta(seconds=9.5),
        ),
    ]
    assert all(span.ended_at > span.started_at for span in spans)


@pytest.mark.asyncio
async def test_diarization_artifact_rejects_zero_duration_provider_turn(monkeypatch):
    start = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    audio_ranges = [_range("wearable", "64b64b64b64b64b64b64b641", start, 30)]

    class QueryField:
        def __eq__(self, value):
            return ("retry_key", value)

    class StoredArtifact:
        retry_key = QueryField()
        find_one = AsyncMock(return_value=None)

    monkeypatch.setattr(processing_artifacts, "DiarizationArtifact", StoredArtifact)

    with pytest.raises(ValueError, match="positive duration"):
        await processing_artifacts.persist_diarization_artifact(
            user_id="user-1",
            audio_ranges=audio_ranges,
            retry_key="pyannote:capture-1:zero",
            provider="pyannote",
            model="community-1",
            segments=[{"start": 2.0, "end": 2.0, "speaker": "SPEAKER_00"}],
            configuration={"window_seconds": 1200},
        )


@pytest.mark.asyncio
async def test_source_transcript_artifact_is_recovered_from_cutover_revision(
    monkeypatch,
):
    captured = {}

    class StoredRevision:
        @classmethod
        async def find_one(cls, query):
            captured["query"] = query
            return SimpleNamespace(transcript_artifact_ids=["raw-smallest-artifact"])

    monkeypatch.setattr(
        processing_artifacts, "ConversationTranscriptRevision", StoredRevision
    )
    version = SimpleNamespace(version_id="smallest-version", metadata={})

    artifact_ids = await processing_artifacts.resolve_transcript_artifact_ids(
        "conversation-1", version
    )

    assert artifact_ids == ["raw-smallest-artifact"]
    assert captured["query"] == {
        "conversation_id": "conversation-1",
        "metadata.source_version_id": "smallest-version",
        "transcript_artifact_ids.0": {"$exists": True},
    }


@pytest.mark.asyncio
async def test_timing_normalization_derives_a_revision_without_mutating_provider_source(
    monkeypatch,
):
    conversation = Conversation.model_construct(
        conversation_id="conversation-1",
        user_id="user-1",
        client_id="client-1",
        audio_total_duration=10.0,
        transcript_versions=[],
        active_transcript_version=None,
        active_transcript_revision_id=None,
        transcript_integrity_error=None,
    )
    source = conversation.add_transcript_version(
        version_id="smallest-raw",
        transcript="hello",
        words=[Conversation.Word(word="hello", start=9.0, end=10.003)],
        segments=[
            Conversation.SpeakerSegment(
                start=9.0,
                end=10.003,
                text="hello",
                speaker="Speaker 0",
            )
        ],
        provider="smallest",
        model="lightning",
    )
    resolve = AsyncMock(return_value=["raw-smallest-artifact"])
    revision = AsyncMock(return_value=SimpleNamespace(revision_id="revision-1"))
    monkeypatch.setattr(
        processing_artifacts, "resolve_transcript_artifact_ids", resolve
    )
    monkeypatch.setattr(processing_artifacts, "persist_conversation_revision", revision)

    normalized = await processing_artifacts.persist_timing_normalized_revision(
        conversation,
        source,
        segments=[
            {
                "start": 9.0,
                "end": 10.0,
                "text": "hello",
                "speaker": "Speaker 0",
            }
        ],
        words=[{"word": "hello", "start": 9.0, "end": 10.0}],
        audio_duration=10.0,
    )

    assert source.words[0].end == 10.003
    assert source.segments[0].end == 10.003
    assert normalized.words[0].end == 10.0
    assert normalized.segments[0].end == 10.0
    assert normalized.provider == "smallest"
    assert normalized.model == "lightning"
    assert normalized.metadata["reprocessing_type"] == "timing_normalization"
    assert normalized.metadata["source_version_id"] == "smallest-raw"
    assert normalized.metadata["transcript_artifact_ids"] == ["raw-smallest-artifact"]
    assert conversation.active_transcript_version == normalized.version_id
    resolve.assert_awaited_once_with("conversation-1", source)
    assert revision.await_args.kwargs["transcript_artifact_ids"] == [
        "raw-smallest-artifact"
    ]


@pytest.mark.asyncio
async def test_word_timing_synthesis_derives_source_without_mutating_provider_payload(
    monkeypatch,
):
    conversation = Conversation.model_construct(
        conversation_id="conversation-1",
        user_id="user-1",
        client_id="client-1",
        audio_total_duration=10.0,
        transcript_versions=[],
        active_transcript_version=None,
        active_transcript_revision_id=None,
        transcript_integrity_error=None,
    )
    source = conversation.add_transcript_version(
        version_id="provider-raw",
        transcript="hello there",
        words=[],
        segments=[
            Conversation.SpeakerSegment(
                start=2.0,
                end=4.0,
                text="hello there",
                speaker="Speaker 0",
            )
        ],
        provider="vibevoice",
        model="vibevoice",
        metadata={"provider_capabilities": {"diarization": True}},
    )
    resolve = AsyncMock(return_value=["raw-provider-artifact"])
    revision = AsyncMock(return_value=SimpleNamespace(revision_id="revision-1"))
    monkeypatch.setattr(
        processing_artifacts, "resolve_transcript_artifact_ids", resolve
    )
    monkeypatch.setattr(processing_artifacts, "persist_conversation_revision", revision)

    timed = await processing_artifacts.persist_word_timed_revision(
        conversation,
        source,
        words=[
            {"word": "hello", "start": 2.0, "end": 3.0, "confidence": 0.0},
            {"word": "there", "start": 3.0, "end": 4.0, "confidence": 0.0},
        ],
        method="segment_clock_estimate",
        audio_duration=10.0,
    )

    assert source.words == []
    assert [word.word for word in timed.words] == ["hello", "there"]
    assert timed.transcript == source.transcript
    assert timed.segments == source.segments
    assert timed.provider == source.provider
    assert timed.model == source.model
    assert timed.metadata["reprocessing_type"] == "word_timing_synthesis"
    assert timed.metadata["source_version_id"] == "provider-raw"
    assert timed.metadata["word_timing"]["method"] == "segment_clock_estimate"
    assert timed.metadata["transcript_artifact_ids"] == ["raw-provider-artifact"]
    assert timed.metadata["provider_capabilities"] == {"diarization": True}
    assert conversation.active_transcript_version == timed.version_id
    resolve.assert_awaited_once_with("conversation-1", source)
    assert revision.await_args.kwargs["transcript_artifact_ids"] == [
        "raw-provider-artifact"
    ]
