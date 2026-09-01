"""Classification-gated dispatch: the latch, its rollback, and the gated close path.

Real MongoDB documents, because the property under test is the unique
``(episode_key, event_type)`` latch — a faked collection would be testing the fake.
The plugin router and the memory queue are faked: what matters here is *whether* they
are called, not what they do.
"""

import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.controllers import queue_controller
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import (
    DirtyEvidenceRange,
    EpisodeDispatchLatch,
    TimelineAudioRange,
    TimelineEpisode,
)
from advanced_omi_backend.services.timeline import dispatch as dispatch_module
from advanced_omi_backend.services.timeline.dispatch import (
    dispatch_classified_episodes,
    dispatch_ready_episodes,
)

USER = "user-dispatch"
START = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def documents(mongo_service):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_timeline_dispatch_db"]
    await init_beanie(
        database=database,
        document_models=[
            TimelineEpisode,
            EpisodeDispatchLatch,
            DirtyEvidenceRange,
            Conversation,
        ],
    )
    for model in (
        TimelineEpisode,
        EpisodeDispatchLatch,
        DirtyEvidenceRange,
        Conversation,
    ):
        await model.delete_all()
    yield database
    await client.drop_database("test_timeline_dispatch_db")
    client.close()


class _Recorder:
    """Stands in for the plugin router and the memory queue."""

    def __init__(self, fail: bool = False):
        self.events: list[dict] = []
        self.memory: list[str] = []
        self.fail = fail

    async def dispatch_plugin_event(self, **kwargs):
        if self.fail:
            raise RuntimeError("no plugin router")
        self.events.append(kwargs)
        return []

    def enqueue_memory_processing(self, conversation_id: str, *args, **kwargs):
        self.memory.append(conversation_id)
        return SimpleNamespace(id=f"memory_{conversation_id}")


@pytest.fixture
def recorder(monkeypatch):
    fake = _Recorder()
    monkeypatch.setattr(dispatch_module, "_fire_memory", _fire_memory_with(fake))
    monkeypatch.setattr(dispatch_module, "_fire_plugins", _fire_plugins_with(fake))
    return fake


def _fire_memory_with(fake: _Recorder):
    async def fire_memory(_episode, conversations):
        for conversation in conversations:
            fake.enqueue_memory_processing(conversation.conversation_id)

    return fire_memory


def _fire_plugins_with(fake: _Recorder):
    async def fire_plugins(episode, conversations):
        await fake.dispatch_plugin_event(
            user_id=episode.user_id,
            episode_key=episode.episode_key,
            conversations=[item.conversation_id for item in conversations],
        )

    return fire_plugins


async def _episode(
    *,
    episode_key: str = "key-one",
    status: str = "settled",
    conversational: bool = True,
    revision: int = 1,
    conversations: list[str] | None = None,
    audio_conversations: list[str] | None = None,
) -> TimelineEpisode:
    episode = TimelineEpisode(
        run_id="rolling:test",
        user_id=USER,
        local_date=date(2026, 8, 15),
        timezone="UTC",
        started_at=START,
        ended_at=START + timedelta(minutes=20),
        kind="chat" if conversational else "media",
        title="Kitchen chat" if conversational else "Background TV",
        summary="A summary of the episode.",
        conversational=conversational,
        status=status,
        pipeline="rolling",
        episode_key=episode_key,
        revision=revision,
        confidence=0.8,
        activity_mode="foreground",
        related_conversation_ids=list(conversations or []),
        audio_ranges=(
            [
                TimelineAudioRange(
                    capture_source_id="capture-one",
                    time_basis="recorded",
                    chunk_ids=["chunk-one"],
                    started_at=START,
                    ended_at=START + timedelta(minutes=20),
                    conversation_ids=list(audio_conversations),
                )
            ]
            if audio_conversations
            else []
        ),
    )
    await episode.insert()
    return episode


async def _conversation(conversation_id: str) -> Conversation:
    conversation = Conversation(
        conversation_id=conversation_id,
        user_id=USER,
        client_id="client",
        started_at=START,
        ended_at=START + timedelta(minutes=20),
        transcript="hello there",
    )
    await conversation.insert()
    return conversation


@pytest.mark.asyncio
async def test_settled_conversational_episode_dispatches_exactly_once(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(conversations=["conv-one"])

    first = await dispatch_classified_episodes(USER, [episode.episode_id])
    second = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert first == {"memory": ["key-one"], "events": ["key-one"]}
    assert second == {"memory": [], "events": []}
    assert len(recorder.events) == 1
    assert recorder.memory == ["conv-one"]
    assert await EpisodeDispatchLatch.find_all().count() == 2


@pytest.mark.asyncio
async def test_provisional_conversational_episode_dispatches_once(documents, recorder):
    await _conversation("conv-one")
    episode = await _episode(status="provisional", conversations=["conv-one"])

    first = await dispatch_classified_episodes(USER, [episode.episode_id])
    second = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert first == {"memory": ["key-one"], "events": []}
    assert second == {"memory": [], "events": []}
    assert recorder.memory == ["conv-one"]
    assert recorder.events == []


@pytest.mark.asyncio
async def test_two_episodes_referencing_one_conversation_extract_memory_once(
    documents, recorder
):
    await _conversation("conv-one")
    first = await _episode(
        episode_key="key-one",
        status="provisional",
        conversations=["conv-one"],
    )
    second = await _episode(
        episode_key="key-two",
        status="provisional",
        conversations=["conv-one"],
    )

    outcome = await dispatch_classified_episodes(
        USER, [first.episode_id, second.episode_id]
    )

    assert outcome == {"memory": ["key-one"], "events": []}
    assert recorder.memory == ["conv-one"]
    latch = await EpisodeDispatchLatch.find_one(
        EpisodeDispatchLatch.event_type == dispatch_module.MEMORY_EXTRACTION
    )
    assert latch is not None
    assert latch.episode_key == "conversation:conv-one"
    assert await dispatch_ready_episodes() == {"unlatched": 0, "dispatched": 0}


@pytest.mark.asyncio
async def test_deleted_related_conversation_never_dispatches_memory(
    documents, recorder
):
    conversation = await _conversation("conv-deleted")
    conversation.deleted = True
    await conversation.save()
    episode = await _episode(
        status="provisional",
        conversations=[conversation.conversation_id],
    )

    outcome = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert outcome == {"memory": [], "events": []}
    assert recorder.memory == []
    assert await dispatch_ready_episodes() == {"unlatched": 0, "dispatched": 0}
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
async def test_recovery_scan_dispatches_only_unlatched_ready_episodes(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(status="provisional", conversations=["conv-one"])

    first = await dispatch_ready_episodes()
    second = await dispatch_ready_episodes()

    assert first == {"unlatched": 1, "dispatched": 1}
    assert second == {"unlatched": 0, "dispatched": 0}
    assert recorder.memory == ["conv-one"]
    latch = await EpisodeDispatchLatch.find_one(
        EpisodeDispatchLatch.episode_key == "conversation:conv-one"
    )
    assert latch is not None


@pytest.mark.asyncio
async def test_recovery_uses_authoritative_audio_range_conversation_ids(
    documents, recorder
):
    """A missing lineage hint must not hide an episode's canonical audio owner."""

    await _conversation("conv-from-audio-range")
    await _episode(
        status="provisional",
        conversations=[],
        audio_conversations=["conv-from-audio-range"],
    )

    recovered = await dispatch_ready_episodes()

    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert recorder.memory == ["conv-from-audio-range"]
    assert await EpisodeDispatchLatch.find_one(
        EpisodeDispatchLatch.episode_key == "conversation:conv-from-audio-range"
    )


@pytest.mark.asyncio
async def test_recovery_scan_repairs_missing_completion_latch_without_repeating_memory(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(status="settled", conversations=["conv-one"])
    await EpisodeDispatchLatch(
        user_id=USER,
        episode_key="conversation:conv-one",
        event_type=dispatch_module.MEMORY_EXTRACTION,
        episode_id=episode.episode_id,
        revision=episode.revision,
    ).insert()

    recovered = await dispatch_ready_episodes()

    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert recorder.memory == []
    assert len(recorder.events) == 1
    assert await EpisodeDispatchLatch.find_all().count() == 2


@pytest.mark.asyncio
async def test_resettling_a_new_revision_of_the_same_key_does_not_refire(
    documents, recorder
):
    first_revision = await _episode(conversations=["conv-one"])
    await _conversation("conv-one")
    await dispatch_classified_episodes(USER, [first_revision.episode_id])

    # A later reconciliation publishes revision 2 of the *same* key.
    second_revision = await _episode(revision=2, conversations=["conv-one"])
    dispatched = await dispatch_classified_episodes(USER, [second_revision.episode_id])

    assert dispatched == {"memory": [], "events": []}
    assert len(recorder.events) == 1


@pytest.mark.asyncio
async def test_media_classified_episode_dispatches_nothing(documents, recorder):
    episode = await _episode(conversational=False, conversations=["conv-one"])

    dispatched = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert dispatched == {"memory": [], "events": []}
    assert recorder.events == []
    assert recorder.memory == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "superseded"])
async def test_unready_episodes_never_dispatch(documents, recorder, status):
    episode = await _episode(status=status, conversations=["conv-one"])

    dispatched = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert dispatched == {"memory": [], "events": []}
    assert recorder.events == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
async def test_dispatch_failure_releases_the_latch_and_a_retry_fires(
    documents, monkeypatch
):
    await _conversation("conv-one")
    episode = await _episode(conversations=["conv-one"])

    failing = _Recorder(fail=True)
    monkeypatch.setattr(dispatch_module, "_fire_memory", _fire_memory_with(failing))
    monkeypatch.setattr(dispatch_module, "_fire_plugins", _fire_plugins_with(failing))
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "memory": ["key-one"],
        "events": [],
    }
    # The successful memory effect stays latched; only the failed plugin latch rolls back.
    assert await EpisodeDispatchLatch.find_all().count() == 1

    healthy = _Recorder()
    monkeypatch.setattr(dispatch_module, "_fire_memory", _fire_memory_with(healthy))
    monkeypatch.setattr(dispatch_module, "_fire_plugins", _fire_plugins_with(healthy))
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "memory": [],
        "events": ["key-one"],
    }
    assert len(healthy.events) == 1
    assert await EpisodeDispatchLatch.find_all().count() == 2


# ── The gated close path ─────────────────────────────────────────────────────


class _QueueFake:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, func, *args, **kwargs):
        self.enqueued.append((getattr(func, "__name__", str(func)), kwargs))
        return SimpleNamespace(id=kwargs.get("job_id", "job"), meta={})

    def __len__(self):
        return len(self.enqueued)


def _close_path_fakes(monkeypatch, pipeline: str):
    """Run the real ``start_post_conversation_jobs`` against fake queues."""

    queues = {name: _QueueFake() for name in ("transcription", "memory", "default")}
    monkeypatch.setattr(
        queue_controller, "transcription_queue", queues["transcription"]
    )
    monkeypatch.setattr(queue_controller, "memory_queue", queues["memory"])
    monkeypatch.setattr(queue_controller, "default_queue", queues["default"])
    monkeypatch.setattr(
        queue_controller, "_clear_post_conversation_chain", lambda _: None
    )
    # RQ's Dependency validates that it holds real Job objects; the fake queue hands
    # back stand-ins, and the wiring under test is which jobs are enqueued at all.
    monkeypatch.setattr(
        queue_controller, "_as_allow_failure_dependency", lambda depends_on: depends_on
    )
    monkeypatch.setattr(
        queue_controller, "schedule_conversation_dirty", lambda *_, **__: None
    )
    monkeypatch.setattr(queue_controller, "publish_sse_event", lambda *_, **__: None)
    monkeypatch.setattr(
        queue_controller, "active_pipeline_sync", lambda _user_id: pipeline
    )
    monkeypatch.setattr(
        queue_controller,
        "enqueue_summary_job_bundle",
        lambda *args, **kwargs: {
            stage: SimpleNamespace(id=stage)
            for stage in ("title", "short_summary", "detailed_summary")
        },
    )
    return queue_controller, queues


def test_rolling_close_path_skips_memory_and_plugin_dispatch(monkeypatch):
    queue_controller, queues = _close_path_fakes(monkeypatch, "rolling")

    jobs = queue_controller.start_post_conversation_jobs("conv-one", "user-one")

    assert jobs["memory"] is None
    assert queues["memory"].enqueued == []
    # The terminal job still runs — it owns end_reason/completed_at/status — but it is
    # the finalize-only variant that dispatches no user-facing event.
    assert [name for name, _ in queues["default"].enqueued] == [
        "finalize_conversation_close_job"
    ]
    # Evidence producers are untouched.
    assert [name for name, _ in queues["transcription"].enqueued] == [
        "recognise_speakers_job"
    ]


def test_rolling_space_close_path_runs_scoped_memory_and_deferred_dispatch(monkeypatch):
    """Spaces do not have Timeline settlement, so their close effects run directly."""

    queue_controller, queues = _close_path_fakes(monkeypatch, "rolling")

    jobs = queue_controller.start_post_conversation_jobs(
        "space-conversation",
        "user-one",
        memory_space_id="5a265801-b8ca-4667-ae7d-07b2c170ecad",
    )

    assert jobs["memory"] == "memory_space-conver"
    assert [name for name, _ in queues["memory"].enqueued] == ["process_memory_job"]
    assert [name for name, _ in queues["default"].enqueued] == [
        "dispatch_conversation_complete_event_job"
    ]


def test_day_close_path_is_unchanged(monkeypatch):
    queue_controller, queues = _close_path_fakes(monkeypatch, "day")

    jobs = queue_controller.start_post_conversation_jobs("conv-one", "user-one")

    assert jobs["memory"] == "memory_conv-one"
    assert [name for name, _ in queues["memory"].enqueued] == ["process_memory_job"]
    assert [name for name, _ in queues["default"].enqueued] == [
        "dispatch_conversation_complete_event_job"
    ]
