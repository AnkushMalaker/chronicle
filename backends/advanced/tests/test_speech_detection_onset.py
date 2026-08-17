"""Speech detection must materialize claims from evidence time, not polling time."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.workers import transcription_jobs


@pytest.mark.asyncio
async def test_speech_detection_job_anchors_conversation_to_first_timed_word(
    monkeypatch,
):
    combined = {
        "text": "hey hermes order swiggy",
        "words": [
            {"word": "hey", "start": 2.0, "end": 2.2},
            {"word": "hermes", "start": 2.2, "end": 2.6},
            {"word": "order", "start": 7.0, "end": 7.3},
            {"word": "swiggy", "start": 7.3, "end": 7.7},
        ],
        "segments": [],
        "chunk_count": 4,
    }
    aggregator = SimpleNamespace(get_combined_results=AsyncMock(return_value=combined))
    recorded_detection_times = []

    class Store:
        def __init__(self, _redis):
            pass

        async def get_conversation_count(self, _session_id):
            return 0

        async def get_transcription_error(self, _session_id):
            return None

        async def get_status(self, _session_id):
            return SessionStatus.ACTIVE

        async def take_close_request(self, _session_id):
            return None

        async def record_event(self, _session_id, _event):
            return None

        async def read(self, _session_id):
            return SimpleNamespace(started_at=1_000.0)

        async def set_speech_detected_at(self, _session_id, detected_at):
            recorded_detection_times.append(detected_at)

    queued = []

    class Queue:
        def enqueue(self, *args, **kwargs):
            queued.append((args, kwargs))
            return SimpleNamespace(id="open-conversation-1")

    current_job = SimpleNamespace(id="speech-job-1", meta={}, save_meta=Mock())
    monkeypatch.setattr(
        transcription_jobs,
        "TranscriptionResultsAggregator",
        lambda _redis: aggregator,
    )
    monkeypatch.setattr(transcription_jobs, "SessionStore", Store)
    monkeypatch.setattr(transcription_jobs, "get_current_job", lambda: current_job)
    monkeypatch.setattr(
        transcription_jobs, "get_live_segmentation", lambda: "streaming_stt"
    )
    monkeypatch.setattr(
        transcription_jobs, "check_job_alive", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        transcription_jobs,
        "analyze_speech",
        lambda _data: {"has_speech": True, "word_count": 4, "duration": 5.7},
    )
    monkeypatch.setattr(transcription_jobs, "transcription_queue", Queue())
    monkeypatch.setattr(transcription_jobs, "update_job_meta", Mock())
    monkeypatch.setattr(transcription_jobs, "set_otel_session", Mock())
    monkeypatch.setattr(transcription_jobs.time, "time", lambda: 2_000.0)
    monkeypatch.setenv("RECORD_ONLY_ENROLLED_SPEAKERS", "false")

    result = await transcription_jobs.stream_speech_detection_job.__wrapped__(
        "capture-1",
        "user-1",
        "client-1",
        redis_client=object(),
    )

    assert recorded_detection_times == [1_002.0]
    assert queued[0][0][4] == 1_002.0
    assert result["speech_detected_at"] == "1970-01-01T00:16:42"
