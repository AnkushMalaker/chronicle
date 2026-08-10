import io
import wave
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from advanced_omi_backend.services.transcript_integrity import (
    TranscriptTimingError,
    validate_and_normalize_transcript_timing,
)


def wav_bytes(frames=800):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def test_speaker_batches_are_bounded_by_count_and_total_duration():
    # Imported here so this test can address private batching behavior.
    from advanced_omi_backend import speaker_recognition_client as module

    segments = [
        {"start": 0.0, "end": 100.0},
        {"start": 100.0, "end": 200.0},
        {"start": 200.0, "end": 300.0},
    ]

    assert module._pack_identification_batches(
        segments,
        [0, 1, 2],
        max_items=32,
        max_seconds=220.0,
    ) == [[0, 1], [2]]


def test_small_provider_overhang_is_clipped_before_storage():
    segments = [{"start": 8.0, "end": 10.7, "text": "last words"}]
    words = [{"start": 9.5, "end": 10.4, "word": "words"}]

    clean_segments, clean_words = validate_and_normalize_transcript_timing(
        segments, words, audio_duration=10.0
    )

    assert clean_segments[0]["end"] == 10.0
    assert clean_words[0]["end"] == 10.0
    # The paid-provider response remains untouched for the content-hash cache.
    assert segments[0]["end"] == 10.7
    assert words[0]["end"] == 10.4


def test_tiny_fully_outside_tail_item_is_discarded():
    segments, words = validate_and_normalize_transcript_timing(
        [{"start": 0.0, "end": 10.0, "text": "speech"}],
        [{"start": 10.002, "end": 10.004, "word": "tail"}],
        audio_duration=10.0,
    )

    assert len(segments) == 1
    assert words == []


def test_stale_transcript_clock_is_rejected():
    with pytest.raises(TranscriptTimingError) as raised:
        validate_and_normalize_transcript_timing(
            [{"start": 868.72, "end": 885.9, "text": "stale"}],
            [],
            audio_duration=130.0,
        )

    assert raised.value.code == "transcript_timing_out_of_bounds"
    assert raised.value.details["audio_duration"] == 130.0
    assert raised.value.details["max_timing"] == 885.9


def test_invalid_segment_order_is_rejected():
    with pytest.raises(TranscriptTimingError) as raised:
        validate_and_normalize_transcript_timing(
            [{"start": 4.0, "end": 3.0, "text": "backwards"}],
            [],
            audio_duration=10.0,
        )

    assert raised.value.code == "transcript_timing_invalid_range"


def test_segment_without_backing_chunk_is_rejected():
    with pytest.raises(TranscriptTimingError) as raised:
        validate_and_normalize_transcript_timing(
            [{"start": 100.94, "end": 102.36, "text": "phantom"}],
            [],
            audio_duration=141.25,
            audio_ranges=[(0.0, 98.0)],
        )

    assert raised.value.code == "transcript_audio_gap"


@pytest.mark.asyncio
async def test_speaker_wrapper_reports_audio_range_failure_as_data_error(monkeypatch):
    # Imported here so monkeypatch targets the module-level dependency.
    from advanced_omi_backend import speaker_recognition_client as module

    async def bad_range(*_args, **_kwargs):
        raise ValueError(
            "Invalid time range: end_time (130.0s) must be > start_time (868.72s)"
        )

    monkeypatch.setattr(module, "reconstruct_audio_ranges", bad_range)
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.identify_batch = AsyncMock()

    result = await client._identify_per_segment(
        conversation_id="conversation",
        segments=[{"start": 868.72, "end": 885.9, "text": "stale", "speaker": "A"}],
        speech_segments=[{"start": 868.72, "end": 885.9}],
        non_speech_indices=set(),
        user_id="user",
        similarity_threshold=0.5,
        min_segment_duration=0.1,
    )

    assert result["error"] == "transcript_data_error"
    assert result["segments"][0]["status"] == "data_error"
    assert "Speaker service" not in result["message"]
    client.identify_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_wrapper_does_not_send_empty_wav_to_service(monkeypatch):
    # Imported here so monkeypatch targets the module-level dependency.
    from advanced_omi_backend import speaker_recognition_client as module

    async def empty_wav(*_args, **_kwargs):
        return b"RIFF" + (b"\x00" * 40)

    identify = AsyncMock()

    async def empty_wavs(*_args, **_kwargs):
        return [await empty_wav()]

    monkeypatch.setattr(module, "reconstruct_audio_ranges", empty_wavs)
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.identify_batch = identify

    result = await client._identify_per_segment(
        conversation_id="conversation",
        segments=[{"start": 100.94, "end": 102.36, "text": "gap", "speaker": "A"}],
        speech_segments=[{"start": 100.94, "end": 102.36}],
        non_speech_indices=set(),
        user_id="user",
        similarity_threshold=0.5,
        min_segment_duration=0.1,
    )

    assert result["error"] == "transcript_data_error"
    identify.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_wrapper_reconstructs_and_identifies_segments_in_one_batch(
    monkeypatch,
):
    # Imported here so monkeypatch targets the module-level dependency.
    from advanced_omi_backend import speaker_recognition_client as module

    reconstruct = AsyncMock(return_value=[wav_bytes(), wav_bytes()])
    identify_batch = AsyncMock(
        return_value={
            "results": [
                {
                    "segment_id": "0",
                    "found": True,
                    "speaker_name": "Aryan",
                    "confidence": 0.9,
                    "status": "identified",
                    "embedding": [0.1, 0.2],
                    "embedding_model": "wespeaker-test",
                },
                {
                    "segment_id": "1",
                    "found": False,
                    "speaker_name": None,
                    "confidence": 0.4,
                    "status": "unknown",
                },
            ]
        }
    )
    monkeypatch.setattr(module, "reconstruct_audio_ranges", reconstruct)
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.identify_batch = identify_batch
    segments = [
        {"start": 1.0, "end": 3.0, "text": "one", "speaker": "A"},
        {"start": 4.0, "end": 6.0, "text": "two", "speaker": "B"},
    ]

    result = await client._identify_per_segment(
        conversation_id="conversation",
        segments=segments,
        speech_segments=segments,
        non_speech_indices=set(),
        user_id="user",
        similarity_threshold=0.5,
        min_segment_duration=0.1,
    )

    reconstruct.assert_awaited_once_with(
        "conversation",
        [(1.0, 3.0), (4.0, 6.0)],
    )
    identify_batch.assert_awaited_once()
    assert [segment["status"] for segment in result["segments"]] == [
        "identified",
        "unknown",
    ]
    assert result["segments"][0]["identified_as"] == "Aryan"
    assert result["segments"][0]["_evaluation_embedding"] == [0.1, 0.2]
    assert result["segments"][0]["_embedding_model"] == "wespeaker-test"


@pytest.mark.asyncio
async def test_speaker_majority_vote_reconstructs_and_identifies_samples_in_one_batch(
    monkeypatch,
):
    # Imported here so monkeypatch targets the module-level dependency.
    from advanced_omi_backend import speaker_recognition_client as module

    reconstruct = AsyncMock(return_value=[wav_bytes(), wav_bytes()])
    identify_batch = AsyncMock(
        return_value={
            "results": [
                {
                    "segment_id": "0",
                    "found": True,
                    "speaker_name": "Aryan",
                    "confidence": 0.9,
                    "status": "identified",
                },
                {
                    "segment_id": "1",
                    "found": True,
                    "speaker_name": "Aryan",
                    "confidence": 0.8,
                    "status": "identified",
                },
            ]
        }
    )
    monkeypatch.setattr(module, "reconstruct_audio_ranges", reconstruct)
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {"similarity_threshold": 0.5},
    )
    client = module.SpeakerRecognitionClient.__new__(module.SpeakerRecognitionClient)
    client.enabled = True
    client.identify_batch = identify_batch
    segments = [
        {"start": 1.0, "end": 4.0, "text": "one", "speaker": "A"},
        {"start": 5.0, "end": 7.0, "text": "two", "speaker": "A"},
    ]

    result = await client.identify_provider_segments(
        "conversation",
        segments,
        user_id="user",
        min_segment_duration=0.1,
    )

    reconstruct.assert_awaited_once_with(
        "conversation",
        [(1.0, 4.0), (5.0, 7.0)],
    )
    identify_batch.assert_awaited_once()
    assert [segment["identified_as"] for segment in result["segments"]] == [
        "Aryan",
        "Aryan",
    ]


@pytest.mark.asyncio
async def test_transcription_job_quarantines_bad_cached_timing(monkeypatch):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from advanced_omi_backend.workers import transcription_jobs as module

    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_total_duration=130.0,
        transcript_integrity_error=None,
        save=AsyncMock(),
    )
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(), find_one=AsyncMock(return_value=conversation)
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(
        module,
        "gather_transcription_context",
        AsyncMock(
            return_value=SimpleNamespace(combined="", hot_words="", user_jargon="")
        ),
    )
    monkeypatch.setattr(
        module,
        "transcribe_audio_range",
        AsyncMock(
            return_value={
                "text": "stale",
                "segments": [{"start": 868.72, "end": 885.9, "text": "stale"}],
                "words": [],
                "provider_name": "smallest",
                "provider_capabilities": {"diarization": True},
                "wav_size": 123,
            }
        ),
    )
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 130.0)]),
    )
    record_event = Mock()
    monkeypatch.setattr(module, "record_event_sync", record_event)

    with pytest.raises(TranscriptTimingError):
        await module.transcribe_full_audio_job.__wrapped__(
            "conversation", "version", trigger="reprocess"
        )

    assert conversation.transcript_integrity_error.startswith(
        "transcript_timing_out_of_bounds:"
    )
    conversation.save.assert_awaited_once()
    record_event.assert_called_once()
    assert record_event.call_args.kwargs["category"] == "data_integrity"


@pytest.mark.asyncio
async def test_registered_speaker_job_runs_batch_provider_identification(monkeypatch):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from advanced_omi_backend.workers import speaker_jobs as module

    source_segment = module.Conversation.SpeakerSegment(
        start=1.0,
        end=4.0,
        text="hello there",
        speaker="SPEAKER_00",
    )
    version = module.Conversation.TranscriptVersion(
        version_id="source",
        transcript="hello there",
        segments=[source_segment],
        provider="smallest",
        created_at=datetime.now(timezone.utc),
        diarization_source="provider",
        metadata={"provider_capabilities": {"diarization": True}},
    )
    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_total_duration=5.0,
        transcript_integrity_error=None,
        get_transcript_version=lambda version_id: version,
        active_transcript=version,
        save=AsyncMock(),
    )
    identify_provider_segments = AsyncMock(
        return_value={
            "segments": [
                {
                    "start": 1.0,
                    "end": 4.0,
                    "text": "hello there",
                    "speaker": "SPEAKER_00",
                    "identified_as": "Aryan",
                    "confidence": 0.9,
                    "status": "identified",
                    "_evaluation_embedding": [1.0, 0.0],
                }
            ]
        }
    )
    fake_client = SimpleNamespace(
        enabled=True,
        identify_provider_segments=identify_provider_segments,
    )
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
        SpeakerSegment=module.Conversation.SpeakerSegment,
        Word=module.Conversation.Word,
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: fake_client)
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 5.0)]),
    )
    monkeypatch.setattr(
        module,
        "get_diarization_settings",
        lambda: {"diarization_source": "provider"},
    )
    monkeypatch.setattr(
        module,
        "get_misc_settings",
        lambda: {"per_segment_speaker_id": True},
    )
    monkeypatch.setattr(module, "get_user_by_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        module, "_human_speaker_annotations", AsyncMock(return_value=[])
    )
    compute_cluster_centroids = AsyncMock(return_value=({}, {}))
    monkeypatch.setattr(module, "compute_cluster_centroids", compute_cluster_centroids)

    result = await module.recognise_speakers_job.__wrapped__(
        "conversation",
        "source",
    )

    assert result["success"] is True
    assert result["identified_speakers"] == ["Aryan"]
    identify_provider_segments.assert_awaited_once_with(
        conversation_id="conversation",
        segments=[
            {
                "start": 1.0,
                "end": 4.0,
                "text": "hello there",
                "speaker": "SPEAKER_00",
            }
        ],
        user_id="user",
        per_segment=True,
        min_segment_duration=0.5,
    )
    conversation.save.assert_awaited_once()
    compute_cluster_centroids.assert_not_awaited()
    assert version.metadata["cluster_centroids"] == {"Aryan": [1.0, 0.0]}


@pytest.mark.asyncio
async def test_registered_speaker_job_skips_event_only_transcript(monkeypatch):
    # Imported here so monkeypatch targets the registered entrypoint module.
    from advanced_omi_backend.workers import speaker_jobs as module

    version = module.Conversation.TranscriptVersion(
        version_id="source",
        transcript="[Silence]",
        segments=[
            module.Conversation.SpeakerSegment(
                start=0.0,
                end=5.0,
                text="[Silence]",
                speaker="",
                segment_type="event",
            )
        ],
        provider="smallest",
        created_at=datetime.now(timezone.utc),
    )
    conversation = SimpleNamespace(
        user_id="user",
        client_id="client",
        audio_total_duration=5.0,
        transcript_integrity_error=None,
        get_transcript_version=lambda version_id: version,
        active_transcript=version,
        save=AsyncMock(),
    )
    fake_client = SimpleNamespace(enabled=True)
    fake_conversation_model = SimpleNamespace(
        conversation_id=object(),
        find_one=AsyncMock(return_value=conversation),
        SpeakerSegment=module.Conversation.SpeakerSegment,
        Word=module.Conversation.Word,
    )
    monkeypatch.setattr(module, "Conversation", fake_conversation_model)
    monkeypatch.setattr(module, "SpeakerRecognitionClient", lambda: fake_client)
    monkeypatch.setattr(
        module,
        "load_transcript_audio_ranges",
        AsyncMock(return_value=[(0.0, 5.0)]),
    )

    result = await module.recognise_speakers_job.__wrapped__(
        "conversation",
        "target",
        source_version_id="source",
    )

    assert result["success"] is True
    assert result["skip_reason"] == "No speech segments to identify"
    conversation.save.assert_not_awaited()


def test_full_diarization_requires_continuous_audio_coverage():
    # Imported here to exercise the worker's exact continuity policy.
    from advanced_omi_backend.workers import speaker_jobs as module

    assert module._audio_ranges_cover_continuously(
        [(0.0, 10.0), (10.1, 20.0)],
        duration=20.0,
    )
    assert not module._audio_ranges_cover_continuously(
        [(0.0, 10.0), (12.0, 20.0)],
        duration=20.0,
    )
    assert not module._audio_ranges_cover_continuously(
        [(2.0, 20.0)],
        duration=20.0,
    )


def test_speaker_job_recovers_spoken_text_mislabeled_as_event():
    # Imported here to exercise the worker's exact speech classification policy.
    from advanced_omi_backend.workers import speaker_jobs as module

    spoken = module.Conversation.SpeakerSegment(
        start=1.0,
        end=4.0,
        text="We should decide which base model to use next.",
        speaker="",
        segment_type="event",
    )
    silence = module.Conversation.SpeakerSegment(
        start=4.0,
        end=6.0,
        text="[Silence]",
        speaker="",
        segment_type="event",
    )

    assert module._is_speech_segment(spoken)
    assert not module._is_speech_segment(silence)
