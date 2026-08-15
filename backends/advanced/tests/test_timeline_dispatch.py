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
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.controllers import queue_controller
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import (
    DirtyEvidenceRange,
    EpisodeDispatchLatch,
    TimelineEpisode,
    utcnow,
)
from advanced_omi_backend.routers.modules import timeline_routes
from advanced_omi_backend.services.timeline import dispatch as dispatch_module
from advanced_omi_backend.services.timeline.dispatch import dispatch_settled_episodes

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
    monkeypatch.setattr(
        dispatch_module, "dispatch_plugin_event", fake.dispatch_plugin_event
    )
    monkeypatch.setattr(
        dispatch_module,
        "_fire",
        _fire_with(fake),
    )
    return fake


def _fire_with(fake: _Recorder):
    """Reuse the real ``_fire`` body but with the two side effects faked."""

    async def _fire(episode, conversations):
        await fake.dispatch_plugin_event(
            user_id=episode.user_id,
            episode_key=episode.episode_key,
            conversations=[item.conversation_id for item in conversations],
        )
        for conversation in conversations:
            fake.enqueue_memory_processing(conversation.conversation_id)

    return _fire


async def _episode(
    *,
    episode_key: str = "key-one",
    status: str = "settled",
    conversational: bool = True,
    revision: int = 1,
    conversations: list[str] | None = None,
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

    first = await dispatch_settled_episodes(USER, [episode.episode_id])
    second = await dispatch_settled_episodes(USER, [episode.episode_id])

    assert first == ["key-one"]
    assert second == []
    assert len(recorder.events) == 1
    assert recorder.memory == ["conv-one"]
    assert await EpisodeDispatchLatch.find_all().count() == 1


@pytest.mark.asyncio
async def test_resettling_a_new_revision_of_the_same_key_does_not_refire(
    documents, recorder
):
    first_revision = await _episode(conversations=["conv-one"])
    await _conversation("conv-one")
    await dispatch_settled_episodes(USER, [first_revision.episode_id])

    # A later reconciliation publishes revision 2 of the *same* key.
    second_revision = await _episode(revision=2, conversations=["conv-one"])
    dispatched = await dispatch_settled_episodes(USER, [second_revision.episode_id])

    assert dispatched == []
    assert len(recorder.events) == 1


@pytest.mark.asyncio
async def test_media_classified_episode_dispatches_nothing(documents, recorder):
    episode = await _episode(conversational=False, conversations=["conv-one"])

    dispatched = await dispatch_settled_episodes(USER, [episode.episode_id])

    assert dispatched == []
    assert recorder.events == []
    assert recorder.memory == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "provisional", "superseded"])
async def test_unsettled_episodes_never_dispatch(documents, recorder, status):
    episode = await _episode(status=status, conversations=["conv-one"])

    dispatched = await dispatch_settled_episodes(USER, [episode.episode_id])

    assert dispatched == []
    assert recorder.events == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
async def test_dispatch_failure_releases_the_latch_and_a_retry_fires(
    documents, monkeypatch
):
    await _conversation("conv-one")
    episode = await _episode(conversations=["conv-one"])

    failing = _Recorder(fail=True)
    monkeypatch.setattr(dispatch_module, "_fire", _fire_with(failing))
    assert await dispatch_settled_episodes(USER, [episode.episode_id]) == []
    # At-least-once: the claim is released so the next settlement can re-fire it.
    assert await EpisodeDispatchLatch.find_all().count() == 0

    healthy = _Recorder()
    monkeypatch.setattr(dispatch_module, "_fire", _fire_with(healthy))
    assert await dispatch_settled_episodes(USER, [episode.episode_id]) == ["key-one"]
    assert len(healthy.events) == 1
    assert await EpisodeDispatchLatch.find_all().count() == 1


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


def test_day_close_path_is_unchanged(monkeypatch):
    queue_controller, queues = _close_path_fakes(monkeypatch, "day")

    jobs = queue_controller.start_post_conversation_jobs("conv-one", "user-one")

    assert jobs["memory"] == "memory_conv-one"
    assert [name for name, _ in queues["memory"].enqueued] == ["process_memory_job"]
    assert [name for name, _ in queues["default"].enqueued] == [
        "dispatch_conversation_complete_event_job"
    ]


# ── POST /api/timeline/reconcile ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_reconcile_creates_a_pending_range(documents, monkeypatch):
    monkeypatch.setattr(
        timeline_routes,
        "mark_evidence_dirty",
        _capture_mark(),
    )
    user = SimpleNamespace(id=USER)

    payload = await timeline_routes.request_range_reconciliation(
        timeline_routes.ReconcileRequest(
            started_at=START, ended_at=START + timedelta(minutes=30)
        ),
        user=user,
    )

    assert payload["state"] == "pending"
    assert payload["not_before"] > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_force_makes_the_range_immediately_due(documents, monkeypatch):
    monkeypatch.setattr(timeline_routes, "mark_evidence_dirty", _capture_mark())
    user = SimpleNamespace(id=USER)

    payload = await timeline_routes.request_range_reconciliation(
        timeline_routes.ReconcileRequest(
            started_at=START, ended_at=START + timedelta(minutes=30), force=True
        ),
        user=user,
    )

    assert payload["not_before"] <= datetime.now(timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "started, ended",
    [
        (START, START),
        (START + timedelta(hours=1), START),
        (START, START + timedelta(hours=7)),
    ],
)
async def test_manual_reconcile_rejects_impossible_ranges(monkeypatch, started, ended):
    monkeypatch.setattr(timeline_routes, "mark_evidence_dirty", _capture_mark())

    with pytest.raises(HTTPException) as error:
        await timeline_routes.request_range_reconciliation(
            timeline_routes.ReconcileRequest(started_at=started, ended_at=ended),
            user=SimpleNamespace(id=USER),
        )

    assert error.value.status_code == 422


def _capture_mark():
    """A ``mark_evidence_dirty`` that records the row it would have written."""

    async def mark(user_id, started_at, ended_at, source_revision, reason, **kwargs):
        not_before = kwargs.get("not_before") or utcnow() + timedelta(minutes=5)
        return SimpleNamespace(
            dirty_range_id="range-one",
            state="pending",
            started_at=started_at,
            ended_at=ended_at,
            not_before=not_before,
            force_after=utcnow() + timedelta(minutes=15),
        )

    return mark
