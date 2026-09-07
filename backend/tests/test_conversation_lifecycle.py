"""Tests for speech-driven Conversation materialization and teardown."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymongo.errors import DuplicateKeyError

from backend.models.audio_capture import AudioRangeRef
from backend.models.conversation import Conversation
from backend.services.audio_stream import conversation_lifecycle
from backend.services.audio_stream.session_store import SessionStatus
from backend.workers import conversation_jobs

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_detected_speech_materializes_after_capture_has_finished(monkeypatch):
    """A final STT result may cross the speech threshold after captureStopped."""

    conversation = SimpleNamespace(
        conversation_id="conversation-1",
        markers=[],
        save=AsyncMock(),
    )
    materialized = SimpleNamespace(conversation=conversation, created=True)

    class _FinishedSessionStore:
        def __init__(self, _redis_client):
            pass

        async def set_active_conversation(self, _session_id, _conversation_id):
            return False

        async def get_active_conversation_id(self, _session_id):
            return None

        async def read(self, _session_id):
            return SimpleNamespace(status=SessionStatus.FINISHED)

        async def get_markers(self, _session_id):
            return []

    current_job = SimpleNamespace(meta={}, save_meta=Mock())
    speech_job = SimpleNamespace(meta={}, save_meta=Mock())

    monkeypatch.setattr(conversation_jobs, "SessionStore", _FinishedSessionStore)
    monkeypatch.setattr(
        conversation_jobs,
        "materialize_detected_conversation",
        AsyncMock(return_value=materialized),
    )
    monkeypatch.setattr(conversation_jobs.Job, "fetch", Mock(return_value=speech_job))
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", Mock())

    result = await conversation_jobs._initialize_conversation(
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
        speech_job_id="speech-job-1",
        speech_detected_at=1_788_029_707.0,
        current_job=current_job,
        redis_client=SimpleNamespace(),
    )

    assert result == "conversation-1"
    assert current_job.meta["conversation_id"] == "conversation-1"
    assert speech_job.meta["conversation_id"] == "conversation-1"


def test_streaming_timestamp_rebase_preserves_real_leading_silence():
    words = [{"word": "hello", "start": 2.08, "end": 2.4}]
    segments = [
        {
            "start": 2.08,
            "end": 2.4,
            "words": [{"word": "hello", "start": 2.08, "end": 2.4}],
        }
    ]

    conversation_jobs._rebase_timestamps_to_conversation_start(
        words,
        segments,
        capture_clock_offset_seconds=0.0,
    )

    assert words[0]["start"] == pytest.approx(2.08)
    assert segments[0]["start"] == pytest.approx(2.08)
    assert segments[0]["words"][0]["start"] == pytest.approx(2.08)


def test_streaming_timestamp_rebase_uses_claimed_audio_origin():
    words = [{"word": "hello", "start": 785.0, "end": 785.4}]
    segments = [
        {
            "start": 785.0,
            "end": 785.4,
            "words": [{"word": "hello", "start": 785.0, "end": 785.4}],
        }
    ]

    conversation_jobs._rebase_timestamps_to_conversation_start(
        words,
        segments,
        capture_clock_offset_seconds=780.0,
    )

    assert words[0]["start"] == pytest.approx(5.0)
    assert segments[0]["start"] == pytest.approx(5.0)
    assert segments[0]["words"][0]["start"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_open_conversation_entrypoint_uses_claim_audio_clock_for_final_words(
    monkeypatch,
):
    started_at = datetime(2026, 8, 21, 6, 48, 32, tzinfo=timezone.utc)
    audio_range = AudioRangeRef(
        capture_source_id="source-1",
        capture_session_ids=["session-1"],
        time_basis="received",
        chunk_ids=["000000000000000000000001"],
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10),
    )

    class _TranscriptConversation:
        add_transcript_version = Conversation.add_transcript_version

        def __init__(self):
            self.conversation_id = "conversation-1"
            self.user_id = "user-1"
            self.client_id = "client-1"
            self.audio_ranges = [audio_range]
            self.started_at = started_at
            self.ended_at = started_at + timedelta(seconds=10)
            self.transcript_versions = []
            self.active_transcript_version = None
            self.save = AsyncMock()

        @property
        def active_transcript(self):
            return next(
                (
                    version
                    for version in self.transcript_versions
                    if version.version_id == self.active_transcript_version
                ),
                None,
            )

    conversation = _TranscriptConversation()

    class _TranscriptConversationModel:
        conversation_id = _QueryField()
        Word = Conversation.Word
        SpeakerSegment = Conversation.SpeakerSegment

        @classmethod
        async def find_one(cls, _query):
            return conversation

    final_transcript = {
        "text": "hello",
        "words": [{"word": "hello", "start": 2.08, "end": 2.4}],
        "segments": [
            {
                "start": 2.08,
                "end": 2.4,
                "text": "hello",
                "speaker": 0,
                "words": [{"word": "hello", "start": 2.08, "end": 2.4}],
            }
        ],
        "chunk_count": 1,
        "provider": "smallest",
        "mode": "streaming",
        "model": "pulse",
    }
    aggregator = SimpleNamespace(
        get_combined_results=AsyncMock(return_value=final_transcript)
    )
    current_job = SimpleNamespace(meta={"stale": True}, save_meta=Mock())
    capture_offset = AsyncMock(return_value=0.0)
    transcript_artifact = AsyncMock(
        return_value=SimpleNamespace(artifact_id="artifact-1")
    )

    class _CompletionStore:
        def __init__(self, _redis_client):
            pass

        async def get_completion_reason(self, _session_id):
            return "user_stopped"

    async def close_immediately(state, *_args):
        state.close_requested_reason = "test"
        state.last_result_count = 1

    monkeypatch.setattr(conversation_jobs, "get_current_job", lambda: current_job)
    monkeypatch.setattr(
        conversation_jobs,
        "_initialize_conversation",
        AsyncMock(return_value="conversation-1"),
    )
    monkeypatch.setattr(
        conversation_jobs, "_monitor_conversation_loop", close_immediately
    )
    monkeypatch.setattr(conversation_jobs, "SessionStore", _CompletionStore)
    monkeypatch.setattr(
        conversation_jobs, "TranscriptionResultsAggregator", lambda _redis: aggregator
    )
    monkeypatch.setattr(conversation_jobs, "Conversation", _TranscriptConversationModel)
    monkeypatch.setattr(
        conversation_jobs,
        "_claim_persisted_capture",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        conversation_jobs, "capture_clock_offset_for_ranges", capture_offset
    )
    monkeypatch.setattr(
        conversation_jobs, "persist_transcript_artifact", transcript_artifact
    )
    monkeypatch.setattr(conversation_jobs, "persist_conversation_revision", AsyncMock())
    monkeypatch.setattr(conversation_jobs, "maybe_trim_silence", AsyncMock())
    monkeypatch.setattr(conversation_jobs, "_enqueue_post_processing", AsyncMock())
    monkeypatch.setattr(
        conversation_jobs,
        "handle_end_of_conversation",
        AsyncMock(return_value={"status": "done"}),
    )

    result = await conversation_jobs.open_conversation_job.__wrapped__(
        "session-1",
        "user-1",
        "client-1",
        started_at.timestamp() + 2.08,
        redis_client=SimpleNamespace(),
    )

    assert result == {"status": "done"}
    capture_offset.assert_awaited_once_with("session-1", [audio_range])
    version = conversation.active_transcript
    assert version.words[0].start == pytest.approx(2.08)
    assert version.segments[0].start == pytest.approx(2.08)
    assert version.metadata["timestamp_clock"] == "conversation"
    assert version.metadata["timestamp_rebase_seconds"] == pytest.approx(0.0)
    assert version.metadata["source_timestamp_clock"] == "capture_session"
    assert version.metadata["capture_session_id"] == "session-1"
    assert version.metadata["transcript_artifact_ids"] == ["artifact-1"]
    artifact_words = transcript_artifact.await_args.kwargs["words"]
    assert artifact_words[0]["start"] == pytest.approx(2.08)
    artifact_metadata = transcript_artifact.await_args.kwargs["raw_response"]
    assert artifact_metadata["source_timestamp_clock"] == "capture_session"
    assert artifact_metadata["timestamp_clock"] == "conversation"
    assert artifact_metadata["timestamp_rebase_seconds"] == pytest.approx(0.0)
    assert artifact_metadata["capture_session_id"] == "session-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_version_created", [False, True])
async def test_monitor_projects_live_mongo_and_sse_to_conversation_clock(
    monkeypatch,
    live_version_created,
):
    started_at = datetime(2026, 8, 21, 6, 48, 32, tzinfo=timezone.utc)
    audio_range = AudioRangeRef(
        capture_source_id="source-1",
        capture_session_ids=["session-1"],
        time_basis="received",
        chunk_ids=["000000000000000000000001"],
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10),
    )
    source_segment = {
        "start": 6.704,
        "end": 7.104,
        "text": "hello",
        "speaker": 0,
        "words": [{"word": "hello", "start": 6.704, "end": 7.104}],
    }
    result = {
        "text": "hello",
        "words": [{"word": "hello", "start": 6.704, "end": 7.104}],
        "segments": [source_segment],
        "confidence": 0.9,
        "provider": "smallest",
    }

    result_calls = 0
    signal_calls = 0

    async def wait_for_results(_redis, _session_id, last_stream_id):
        nonlocal result_calls
        result_calls += 1
        if result_calls == 1:
            return [result], "1-0"
        await asyncio.sleep(0.05)
        return [], last_stream_id

    async def wait_for_signal(_pubsub):
        nonlocal signal_calls
        signal_calls += 1
        if signal_calls == 1:
            await asyncio.sleep(0.05)
            return None
        return {"type": "finalize", "reason": "test"}

    class _PubSub:
        subscribe = AsyncMock()
        unsubscribe = AsyncMock()
        aclose = AsyncMock()

    class _Redis:
        def __init__(self):
            self.live_pubsub = _PubSub()

        def pubsub(self):
            return self.live_pubsub

    class _ConversationField:
        def __eq__(self, value):
            return value

    class _LiveConversationModel:
        conversation_id = _ConversationField()

        @classmethod
        async def find_one(cls, _query):
            return SimpleNamespace(audio_ranges=[], started_at=started_at)

    claim_window = AsyncMock(return_value=[audio_range])
    capture_offset = AsyncMock(return_value=4.624)
    create_live = AsyncMock()
    update_live = AsyncMock()
    publish_sse = Mock()
    plugin_router = SimpleNamespace(dispatch_event=AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_jobs, "Conversation", _LiveConversationModel)
    monkeypatch.setattr(conversation_jobs, "SessionStore", lambda _redis: object())
    monkeypatch.setattr(conversation_jobs, "get_live_segmentation", lambda: "provider")
    monkeypatch.setattr(conversation_jobs, "_wait_for_new_results", wait_for_results)
    monkeypatch.setattr(conversation_jobs, "_wait_for_signal", wait_for_signal)
    monkeypatch.setattr(
        conversation_jobs,
        "analyze_speech",
        lambda _data: {
            "has_speech": True,
            "duration": 0.4,
            "speech_end": 7.104,
            "word_count": 1,
        },
    )
    monkeypatch.setattr(
        conversation_jobs,
        "track_speech_activity",
        AsyncMock(return_value=(7.104, 1)),
    )
    monkeypatch.setattr(conversation_jobs, "update_job_progress_metadata", AsyncMock())
    monkeypatch.setattr(conversation_jobs, "publish_sse_event_throttled", Mock())
    monkeypatch.setattr(
        conversation_jobs, "check_job_alive", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(conversation_jobs, "claim_capture_window", claim_window)
    monkeypatch.setattr(
        conversation_jobs, "capture_clock_offset_for_ranges", capture_offset
    )
    monkeypatch.setattr(
        conversation_jobs, "_create_live_transcript_version", create_live
    )
    monkeypatch.setattr(conversation_jobs, "_update_live_transcript", update_live)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", publish_sse)
    monkeypatch.setattr(conversation_jobs, "get_plugin_router", lambda: plugin_router)

    state = conversation_jobs.ConversationState(
        conversation_id="conversation-1",
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
        start_time=conversation_jobs.time.time(),
        live_version_created=live_version_created,
    )
    await conversation_jobs._monitor_conversation_loop(
        state,
        aggregator=object(),
        current_job=SimpleNamespace(),
        redis_client=_Redis(),
    )

    write = update_live if live_version_created else create_live
    other_write = create_live if live_version_created else update_live
    write.assert_awaited_once()
    other_write.assert_not_awaited()
    projected_segments = write.await_args.kwargs["validated_segments"]
    assert projected_segments[0]["start"] == pytest.approx(2.08)
    assert projected_segments[0]["words"][0]["start"] == pytest.approx(2.08)
    assert write.await_args.kwargs["capture_session_id"] == "session-1"
    assert write.await_args.kwargs["capture_clock_offset_seconds"] == pytest.approx(
        4.624
    )

    live_event = next(
        call.args[2]
        for call in publish_sse.call_args_list
        if call.args[1] == "transcript.live"
    )
    assert live_event["segments"][0]["start"] == pytest.approx(2.08)
    assert live_event["source_timestamp_clock"] == "capture_session"
    assert live_event["timestamp_clock"] == "conversation"
    assert live_event["timestamp_rebase_seconds"] == pytest.approx(4.624)
    assert live_event["capture_session_id"] == "session-1"
    plugin_event = plugin_router.dispatch_event.await_args.kwargs["data"]
    assert plugin_event["segments"][0]["start"] == pytest.approx(2.08)
    assert plugin_event["source_timestamp_clock"] == "capture_session"
    assert plugin_event["timestamp_clock"] == "conversation"
    assert plugin_event["timestamp_rebase_seconds"] == pytest.approx(4.624)
    assert source_segment["start"] == pytest.approx(6.704)
    claim_window.assert_awaited_once()
    capture_offset.assert_awaited_once_with("session-1", [audio_range])


@pytest.mark.asyncio
async def test_monitor_withholds_timed_live_outputs_when_clock_prefix_is_unproven(
    monkeypatch,
):
    source_segment = {
        "start": 6.704,
        "end": 7.104,
        "text": "hello",
        "speaker": 0,
        "words": [{"word": "hello", "start": 6.704, "end": 7.104}],
    }
    result = {
        "text": "hello",
        "words": [{"word": "hello", "start": 6.704, "end": 7.104}],
        "segments": [source_segment],
        "confidence": 0.9,
        "provider": "smallest",
    }
    result_calls = 0
    signal_calls = 0

    async def wait_for_results(_redis, _session_id, last_stream_id):
        nonlocal result_calls
        result_calls += 1
        if result_calls == 1:
            return [result], "1-0"
        await asyncio.sleep(0.05)
        return [], last_stream_id

    async def wait_for_signal(_pubsub):
        nonlocal signal_calls
        signal_calls += 1
        if signal_calls == 1:
            await asyncio.sleep(0.05)
            return None
        return {"type": "finalize", "reason": "test"}

    class _PubSub:
        subscribe = AsyncMock()
        unsubscribe = AsyncMock()
        aclose = AsyncMock()

    class _Redis:
        def __init__(self):
            self.live_pubsub = _PubSub()

        def pubsub(self):
            return self.live_pubsub

    create_live = AsyncMock()
    update_live = AsyncMock()
    publish_sse = Mock()
    plugin_router = SimpleNamespace(dispatch_event=AsyncMock(return_value=[]))
    resolve_clock = AsyncMock(
        side_effect=conversation_jobs.AudioClaimError("incomplete audio-clock prefix")
    )
    monkeypatch.setattr(conversation_jobs, "SessionStore", lambda _redis: object())
    monkeypatch.setattr(conversation_jobs, "get_live_segmentation", lambda: "provider")
    monkeypatch.setattr(conversation_jobs, "_wait_for_new_results", wait_for_results)
    monkeypatch.setattr(conversation_jobs, "_wait_for_signal", wait_for_signal)
    monkeypatch.setattr(
        conversation_jobs,
        "analyze_speech",
        lambda _data: {
            "has_speech": True,
            "duration": 0.4,
            "speech_end": 7.104,
            "word_count": 1,
        },
    )
    monkeypatch.setattr(
        conversation_jobs,
        "track_speech_activity",
        AsyncMock(return_value=(7.104, 1)),
    )
    monkeypatch.setattr(conversation_jobs, "update_job_progress_metadata", AsyncMock())
    monkeypatch.setattr(conversation_jobs, "publish_sse_event_throttled", Mock())
    monkeypatch.setattr(
        conversation_jobs, "check_job_alive", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        conversation_jobs, "_resolve_live_capture_clock_offset", resolve_clock
    )
    monkeypatch.setattr(
        conversation_jobs, "_create_live_transcript_version", create_live
    )
    monkeypatch.setattr(conversation_jobs, "_update_live_transcript", update_live)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", publish_sse)
    monkeypatch.setattr(conversation_jobs, "get_plugin_router", lambda: plugin_router)

    state = conversation_jobs.ConversationState(
        conversation_id="conversation-1",
        session_id="session-1",
        user_id="user-1",
        client_id="client-1",
        start_time=conversation_jobs.time.time(),
    )
    await conversation_jobs._monitor_conversation_loop(
        state,
        aggregator=object(),
        current_job=SimpleNamespace(),
        redis_client=_Redis(),
    )

    resolve_clock.assert_awaited_once_with(state)
    create_live.assert_not_awaited()
    update_live.assert_not_awaited()
    plugin_router.dispatch_event.assert_not_awaited()
    assert not any(
        call.args[1] == "transcript.live" for call in publish_sse.call_args_list
    )
    assert source_segment["start"] == pytest.approx(6.704)


class _QueryField:
    def __eq__(self, value):
        return value


class _CaptureModel:
    capture_session_id = _QueryField()
    result = None

    @classmethod
    async def find_one(cls, _query):
        return cls.result


class _ConversationModel:
    segmentation_key = _QueryField()
    result = None

    @classmethod
    async def find_one(cls, _query):
        return cls.result


@pytest.fixture(autouse=True)
def _reset_models(monkeypatch):
    _CaptureModel.result = None
    _ConversationModel.result = None
    monkeypatch.setattr(conversation_lifecycle, "AudioCaptureSession", _CaptureModel)
    monkeypatch.setattr(conversation_lifecycle, "Conversation", _ConversationModel)


@pytest.mark.asyncio
async def test_detected_materialization_requires_a_capture_session():
    with pytest.raises(RuntimeError, match="does not exist"):
        await conversation_lifecycle.materialize_detected_conversation(
            capture_session_id="capture-1",
            user_id="user-1",
            client_id="client-1",
            speech_detected_at=1_786_528_800.0,
        )


@pytest.mark.asyncio
async def test_detected_materialization_rejects_capture_identity_mismatch():
    _CaptureModel.result = SimpleNamespace(
        user_id="other-user",
        client_id="client-1",
        started_at=datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        await conversation_lifecycle.materialize_detected_conversation(
            capture_session_id="capture-1",
            user_id="user-1",
            client_id="client-1",
            speech_detected_at=1_786_528_800.0,
        )


@pytest.mark.asyncio
async def test_detected_materialization_creates_only_after_speech(monkeypatch):
    captured_at = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    _CaptureModel.result = SimpleNamespace(
        user_id="user-1", client_id="client-1", started_at=captured_at
    )
    created = SimpleNamespace(conversation_id="conversation-1", insert=AsyncMock())
    factory = Mock(return_value=created)
    monkeypatch.setattr(conversation_lifecycle, "create_conversation", factory)

    result = await conversation_lifecycle.materialize_detected_conversation(
        capture_session_id="capture-1",
        user_id="user-1",
        client_id="client-1",
        speech_detected_at=captured_at.timestamp() + 20,
        pre_roll_seconds=5,
    )

    assert result.created is True
    assert result.conversation is created
    created.insert.assert_awaited_once()
    kwargs = factory.call_args.kwargs
    assert kwargs["origin"] == "detected"
    assert kwargs["started_at"] == captured_at.replace(second=15)
    assert kwargs["segmentation_key"].startswith("detected:capture-1:")


@pytest.mark.asyncio
async def test_detected_materialization_is_idempotent(monkeypatch):
    captured_at = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    _CaptureModel.result = SimpleNamespace(
        user_id="user-1", client_id="client-1", started_at=captured_at
    )
    existing = SimpleNamespace(conversation_id="conversation-existing")
    _ConversationModel.result = existing
    factory = Mock()
    monkeypatch.setattr(conversation_lifecycle, "create_conversation", factory)

    result = await conversation_lifecycle.materialize_detected_conversation(
        capture_session_id="capture-1",
        user_id="user-1",
        client_id="client-1",
        speech_detected_at=captured_at.timestamp() + 20,
    )

    assert result.created is False
    assert result.conversation is existing
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_insert_race_returns_segmentation_winner(monkeypatch):
    captured_at = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    _CaptureModel.result = SimpleNamespace(
        user_id="user-1", client_id="client-1", started_at=captured_at
    )
    candidate = SimpleNamespace(
        conversation_id="candidate",
        insert=AsyncMock(side_effect=DuplicateKeyError("duplicate")),
    )
    winner = SimpleNamespace(conversation_id="winner")
    calls = 0

    async def find_one(_query):
        nonlocal calls
        calls += 1
        return None if calls == 1 else winner

    monkeypatch.setattr(_ConversationModel, "find_one", find_one)
    monkeypatch.setattr(
        conversation_lifecycle, "create_conversation", Mock(return_value=candidate)
    )

    result = await conversation_lifecycle.materialize_detected_conversation(
        capture_session_id="capture-1",
        user_id="user-1",
        client_id="client-1",
        speech_detected_at=captured_at.timestamp() + 20,
    )

    assert result.created is False
    assert result.conversation is winner


class _EndRedis:
    def __init__(self):
        self.deleted = []

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


class _EndStore:
    status = SessionStatus.ACTIVE
    last_instance = None

    def __init__(self, _redis_client):
        self.cleared = []
        self.expired = False
        self.persisted = False
        type(self).last_instance = self

    async def clear_active_conversation(self, session_id, *, expected_id=None):
        self.cleared.append((session_id, expected_id))
        return True

    async def increment_conversation_count(self, _session_id):
        return 1

    async def get_status_ws_reason(self, _session_id):
        return self.status, self.status == SessionStatus.ACTIVE, ""

    async def persist_session(self, _session_id):
        self.persisted = True

    async def expire_session(self, _session_id, _ttl):
        self.expired = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expects_restart"),
    ((SessionStatus.ACTIVE, True), (SessionStatus.FINALIZING, False)),
)
async def test_conversation_end_clears_semantic_pointer_and_rearms_only_active_session(
    monkeypatch, status, expects_restart
):
    class FakeConversationModel:
        conversation_id = _QueryField()
        EndReason = conversation_jobs.Conversation.EndReason

        @classmethod
        async def find_one(cls, _query):
            return SimpleNamespace(end_reason=None, completed_at=None, save=AsyncMock())

    _EndStore.status = status
    enqueue_speech = Mock()
    monkeypatch.setattr(conversation_jobs, "SessionStore", _EndStore)
    monkeypatch.setattr(conversation_jobs, "Conversation", FakeConversationModel)
    monkeypatch.setattr(conversation_jobs, "enqueue_speech_detection", enqueue_speech)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *args: None)

    await conversation_jobs.handle_end_of_conversation(
        session_id="session-1",
        conversation_id="conversation-1",
        client_id="client-1",
        user_id="user-1",
        start_time=0.0,
        last_result_count=1,
        timeout_triggered=True,
        redis_client=_EndRedis(),
        end_reason="inactivity_timeout",
    )

    store = _EndStore.last_instance
    assert store.cleared == [("session-1", "conversation-1")]
    assert enqueue_speech.call_count == int(expects_restart)
