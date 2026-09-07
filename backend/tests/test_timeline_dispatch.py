"""Classification-gated dispatch: the latch, its rollback, and the gated close path.

Real MongoDB documents, because the property under test is the unique
``(episode_key, event_type)`` latch — a faked collection would be testing the fake.
The plugin router and the memory queue are faked: what matters here is *whether* they
are called, not what they do.
"""

import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from backend.controllers import queue_controller
from backend.models.conversation import Conversation
from backend.models.timeline import (
    DirtyEvidenceRange,
    EpisodeDispatchLatch,
    EpisodeRevisionRef,
    EvidenceLocator,
    TimelineAudioRange,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    TimelinePublicationDayPlan,
    TimelinePublicationJournal,
)
from backend.services.timeline import dispatch as dispatch_module
from backend.services.timeline.dispatch import (
    dispatch_classified_episodes,
    dispatch_ready_episodes,
)
from backend.services.timeline.episode_summary import episode_summary_scope_hash
from backend.services.timeline.publication import publication_identity
from backend.services.timeline.snapshots import build_day_snapshot
from backend.workers import conversation_jobs

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
            TimelineDay,
            TimelinePublicationJournal,
            EpisodeDispatchLatch,
            DirtyEvidenceRange,
            Conversation,
        ],
    )
    for model in (
        TimelineEpisode,
        TimelineDay,
        TimelinePublicationJournal,
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
        self.summaries: list[tuple[str, int]] = []
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
    monkeypatch.setattr(dispatch_module, "_fire_plugins", _fire_plugins_with(fake))
    monkeypatch.setattr(
        dispatch_module,
        "_enqueue_episode_summary_job",
        _enqueue_episode_summary_job_with(fake),
    )
    return fake


def _enqueue_episode_summary_job_with(fake: _Recorder):
    async def enqueue_episode_summary_job(
        episode, *, scope_hash, event_type, claim_token
    ):
        fake.summaries.append((episode.episode_id, episode.revision))

    return enqueue_episode_summary_job


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
    confirmed_fields: list[str] | None = None,
    started_at: datetime = START,
    ended_at: datetime | None = None,
    publish: bool = True,
    dispatch_pending: bool = True,
) -> TimelineEpisode:
    ended_at = ended_at or started_at + timedelta(minutes=20)
    episode = TimelineEpisode(
        run_id="rolling:test",
        user_id=USER,
        local_date=date(2026, 8, 15),
        timezone="UTC",
        started_at=started_at,
        ended_at=ended_at,
        kind="chat" if conversational else "media",
        title="Kitchen chat" if conversational else "Background TV",
        summary="A summary of the episode.",
        conversational=conversational,
        status=status,
        episode_key=episode_key,
        revision=revision,
        confidence=0.8,
        activity_mode="foreground",
        related_conversation_ids=list(conversations or []),
        confirmed_fields=list(confirmed_fields or []),
        evidence_refs=[
            TimelineEvidenceRef(
                evidence_id=f"transcript:{conversation_id}",
                kind="transcript",
                source_id="capture-one",
                source_item_id=conversation_id,
                started_at=started_at,
                ended_at=ended_at,
                role="user_statement",
                excerpt="Speaker: hello there",
                content_hash=f"transcript-hash:{conversation_id}",
                locator=EvidenceLocator(
                    capture_source_id="capture-one",
                    modality="transcript",
                    track_id="input",
                ),
                metadata={"conversation_id": conversation_id},
            )
            for conversation_id in (conversations or audio_conversations or [])
        ],
        audio_ranges=(
            [
                TimelineAudioRange(
                    capture_source_id="capture-one",
                    time_basis="recorded",
                    chunk_ids=["chunk-one"],
                    started_at=started_at,
                    ended_at=ended_at,
                    conversation_ids=list(audio_conversations),
                )
            ]
            if audio_conversations
            else []
        ),
    )
    await episode.insert()
    if publish:
        await _publish_episode(
            episode,
            [episode.local_date],
            dispatch_pending=dispatch_pending,
        )
    return episode


async def _publish_episode(
    episode: TimelineEpisode,
    local_dates: list[date],
    *,
    journal_status: str = "committed",
    dispatch_pending: bool = True,
) -> TimelinePublicationJournal:
    plans: list[TimelinePublicationDayPlan] = []
    for local_date in local_dates:
        day = await TimelineDay.find_one(
            TimelineDay.user_id == USER,
            TimelineDay.local_date == local_date,
            TimelineDay.timezone == episode.timezone,
        )
        refs = (
            []
            if day is None or day.current_snapshot is None
            else list(day.current_snapshot.episode_revisions)
        )
        refs = [ref for ref in refs if ref.episode_key != episode.episode_key]
        refs.append(
            EpisodeRevisionRef(
                episode_key=episode.episode_key,
                revision=episode.revision,
            )
        )
        snapshot = build_day_snapshot(
            user_id=USER,
            local_date=local_date,
            timezone_name=episode.timezone,
            evidence_state_hash="e" * 64,
            episode_revisions=refs,
        )
        plans.append(
            TimelinePublicationDayPlan(
                local_date=local_date,
                timezone=episode.timezone,
                base_snapshot_id=day.current_snapshot_id if day else None,
                resulting_snapshot=snapshot,
            )
        )
        if day is None:
            await TimelineDay(
                user_id=USER,
                local_date=local_date,
                timezone=episode.timezone,
                current_snapshot=snapshot,
                current_snapshot_id=snapshot.snapshot_id,
                snapshot_state="ready",
            ).insert()
        else:
            day.current_snapshot = snapshot
            day.current_snapshot_id = snapshot.snapshot_id
            day.snapshot_state = "ready"
            await day.save()

    publication_id, intent_hash = publication_identity(
        user_id=USER,
        operation_source="projection",
        affected_days=plans,
        operations=[],
    )
    journal = TimelinePublicationJournal(
        publication_id=publication_id,
        intent_hash=intent_hash,
        user_id=USER,
        operation_source="projection",
        affected_days=plans,
        status=journal_status,
        committed_at=START if journal_status == "committed" else None,
        dispatch_pending=dispatch_pending,
    )
    await journal.insert()
    return journal


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


def _summary_job_args(episode: TimelineEpisode, scope_hash: str, token: str = "test"):
    return (
        episode.episode_id,
        episode.revision,
        scope_hash,
        f"episode.detailed_summary:{episode.revision}:{scope_hash}",
        token,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "initial_status"),
    [
        pytest.param("provisional", "provisional", id="provisional"),
        pytest.param("superseded", "superseded", id="superseded"),
        pytest.param("structurally_revised", "settled", id="structurally-revised"),
    ],
)
async def test_registered_episode_summary_job_cannot_land_stale_work(
    documents, monkeypatch, case, initial_status
):
    conversation = await _conversation("conv-summary")
    conversation.add_transcript_version("summary-v1", "hello there")
    await conversation.save()
    episode = await _episode(
        status=initial_status,
        conversations=[conversation.conversation_id],
    )
    scope_hash = episode_summary_scope_hash(episode)
    generated = []

    async def generate(_transcript):
        generated.append(case)
        if case == "structurally_revised":
            # Publish a new exact revision into the ready day snapshot without relying
            # on the old row's superseded flag. Snapshot membership must fence rev 1.
            await _episode(revision=2, conversations=[conversation.conversation_id])
        return "summary computed from stale inputs"

    monkeypatch.setattr(conversation_jobs, "generate_detailed_summary", generate)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *_args: None)

    result = await conversation_jobs.generate_episode_detailed_summary_job.__wrapped__(
        *_summary_job_args(episode, scope_hash),
    )

    stored = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode.episode_id,
        TimelineEpisode.revision == episode.revision,
    )
    assert result["skipped"] is True
    assert stored is not None
    assert stored.detailed_summary is None
    assert stored.detailed_summary_scope_hash is None
    assert generated == ([] if case in {"provisional", "superseded"} else [case])
    if case == "provisional":
        assert result["reason"] == "structure_not_stable"
    if case == "structurally_revised":
        assert result["stale"] is True


@pytest.mark.asyncio
async def test_registered_summary_job_lands_for_confirmed_provisional_revision(
    documents, monkeypatch
):
    conversation = await _conversation("conv-confirmed")
    conversation.add_transcript_version("confirmed-v1", "hello there")
    await conversation.save()
    episode = await _episode(
        status="provisional",
        conversations=[conversation.conversation_id],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
    )
    scope_hash = episode_summary_scope_hash(episode)
    generate = AsyncMock(return_value="Bounded confirmed account")
    monkeypatch.setattr(conversation_jobs, "generate_detailed_summary", generate)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *_args: None)

    result = await conversation_jobs.generate_episode_detailed_summary_job.__wrapped__(
        *_summary_job_args(episode, scope_hash),
    )

    stored = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode.episode_id,
        TimelineEpisode.revision == episode.revision,
    )
    assert result["success"] is True
    assert result["detailed_summary"] == "Bounded confirmed account"
    assert stored.detailed_summary == "Bounded confirmed account"
    assert stored.detailed_summary_scope_hash == scope_hash
    assert stored.detailed_summary_revision == episode.revision
    assert stored.detailed_summary_generated_at is not None

    duplicate = (
        await conversation_jobs.generate_episode_detailed_summary_job.__wrapped__(
            *_summary_job_args(episode, scope_hash, "duplicate-claim"),
        )
    )
    assert duplicate["already_materialized"] is True
    assert generate.await_count == 1


@pytest.mark.asyncio
async def test_registered_summary_job_uses_published_transcript_during_source_change(
    documents, monkeypatch
):
    conversation = await _conversation("conv-immutable-input")
    conversation.add_transcript_version("source-v1", "mutable source before")
    await conversation.save()
    episode = await _episode(conversations=[conversation.conversation_id])
    episode.evidence_refs[0].excerpt = "Speaker: exact published evidence"
    episode.evidence_refs[0].content_hash = "published-evidence-v1"
    await episode.save()
    scope_hash = episode_summary_scope_hash(episode)
    seen_transcripts: list[str] = []

    async def generate(transcript: str) -> str:
        seen_transcripts.append(transcript)
        conversation.add_transcript_version("source-v2", "mutable source changed")
        await conversation.save()
        return "Summary of the published evidence"

    monkeypatch.setattr(conversation_jobs, "generate_detailed_summary", generate)
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *_args: None)

    result = await conversation_jobs.generate_episode_detailed_summary_job.__wrapped__(
        *_summary_job_args(episode, scope_hash)
    )

    stored = await TimelineEpisode.get(episode.id)
    assert result["success"] is True
    assert stored is not None
    assert stored.detailed_summary == "Summary of the published evidence"
    assert stored.detailed_summary_revision == episode.revision
    assert len(seen_transcripts) == 1
    assert "exact published evidence" in seen_transcripts[0]
    assert "mutable source" not in seen_transcripts[0]


@pytest.mark.asyncio
async def test_failed_summary_worker_releases_dispatch_for_recovery(
    documents, monkeypatch
):
    conversation = await _conversation("conv-retry")
    conversation.add_transcript_version("retry-v1", "hello there")
    await conversation.save()
    episode = await _episode(
        status="provisional",
        conversations=[conversation.conversation_id],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
    )
    queued = []

    class Queue:
        def enqueue(self, function, *args, **kwargs):
            queued.append((function, args, kwargs))
            return SimpleNamespace(id=kwargs["job_id"])

    monkeypatch.setattr(queue_controller, "summary_queue", Queue())
    monkeypatch.setattr(
        conversation_jobs,
        "generate_detailed_summary",
        AsyncMock(side_effect=RuntimeError("model process crashed")),
    )

    first = await dispatch_classified_episodes(USER, [episode.episode_id])
    assert first == {"summaries": [episode.episode_key], "events": []}
    assert len(queued) == 1
    function, args, _kwargs = queued[0]
    with pytest.raises(RuntimeError, match="model process crashed"):
        await function.__wrapped__(*args)

    assert await EpisodeDispatchLatch.find_all().count() == 0
    recovered = await dispatch_ready_episodes()
    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert len(queued) == 2

    monkeypatch.setattr(
        conversation_jobs,
        "generate_detailed_summary",
        AsyncMock(return_value="Recovered account"),
    )
    function, args, _kwargs = queued[1]
    result = await function.__wrapped__(*args)
    assert result["success"] is True

    stored = await TimelineEpisode.get(episode.id)
    assert stored is not None
    assert stored.detailed_summary == "Recovered account"
    assert await EpisodeDispatchLatch.find_all().count() == 0
    assert await dispatch_ready_episodes() == {"unlatched": 0, "dispatched": 0}
    assert len(queued) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("race_kind", ["episode", "snapshot"])
async def test_registered_summary_job_rolls_back_an_input_race_at_final_write(
    documents, monkeypatch, race_kind
):
    conversation = await _conversation("conv-final-write-race")
    conversation.add_transcript_version("race-v1", "hello there")
    await conversation.save()
    episode = await _episode(
        conversations=[conversation.conversation_id],
    )
    scope_hash = episode_summary_scope_hash(episode)
    monkeypatch.setattr(
        conversation_jobs,
        "generate_detailed_summary",
        AsyncMock(return_value="Stale account"),
    )
    monkeypatch.setattr(conversation_jobs, "publish_sse_event", lambda *_args: None)
    collection = TimelineEpisode.get_pymongo_collection()

    class RacingCollection:
        def __init__(self):
            self.injected = False

        def __getattr__(self, name):
            return getattr(collection, name)

        async def update_one(self, query, update, *args, **kwargs):
            if (
                not self.injected
                and "$set" in update
                and "detailed_summary" in update["$set"]
            ):
                self.injected = True
                if race_kind == "episode":
                    await collection.update_one(
                        {"_id": episode.id},
                        {"$set": {"revised_at": START + timedelta(seconds=1)}},
                    )
                else:
                    await _episode(
                        revision=2,
                        conversations=[conversation.conversation_id],
                    )
            return await collection.update_one(query, update, *args, **kwargs)

    racing = RacingCollection()
    monkeypatch.setattr(
        TimelineEpisode,
        "get_pymongo_collection",
        classmethod(lambda cls: racing),
    )

    result = await conversation_jobs.generate_episode_detailed_summary_job.__wrapped__(
        *_summary_job_args(episode, scope_hash, "claim-race"),
    )

    stored = await collection.find_one({"_id": episode.id})
    assert racing.injected is True
    assert result["stale"] is True
    assert stored.get("detailed_summary") is None
    assert stored.get("detailed_summary_scope_hash") is None
    assert stored.get("detailed_summary_revision") is None
    assert stored.get("detailed_summary_generated_at") is None


@pytest.mark.asyncio
async def test_settled_conversational_episode_dispatches_exactly_once(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(conversations=["conv-one"])

    first = await dispatch_classified_episodes(USER, [episode.episode_id])
    second = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert first == {"summaries": ["key-one"], "events": ["key-one"]}
    assert second == {"summaries": [], "events": []}
    assert len(recorder.events) == 1
    assert recorder.memory == []
    assert await EpisodeDispatchLatch.find_all().count() == 2


@pytest.mark.asyncio
async def test_provisional_conversational_episode_dispatches_nothing(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(status="provisional", conversations=["conv-one"])

    first = await dispatch_classified_episodes(USER, [episode.episode_id])
    second = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert first == {"summaries": [], "events": []}
    assert second == {"summaries": [], "events": []}
    assert recorder.memory == []
    assert recorder.summaries == []
    assert recorder.events == []


@pytest.mark.asyncio
async def test_structurally_confirmed_provisional_dispatches_summary_not_completion(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(
        status="provisional",
        conversations=["conv-one"],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
    )

    first = await dispatch_classified_episodes(USER, [episode.episode_id])
    second = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert first == {"summaries": ["key-one"], "events": []}
    assert second == {"summaries": [], "events": []}
    assert recorder.summaries == [(episode.episode_id, episode.revision)]
    assert recorder.events == []
    assert await EpisodeDispatchLatch.find_all().count() == 1


@pytest.mark.asyncio
async def test_new_episode_revision_regenerates_detailed_summary(documents, recorder):
    await _conversation("conv-one")
    first = await _episode(
        episode_key="same-event", revision=1, conversations=["conv-one"]
    )
    await dispatch_classified_episodes(USER, [first.episode_id])

    first.status = "superseded"
    await first.save()
    second = await _episode(
        episode_key="same-event", revision=2, conversations=["conv-one"]
    )
    await dispatch_classified_episodes(USER, [second.episode_id])

    assert recorder.summaries == [
        (first.episode_id, 1),
        (second.episode_id, 2),
    ]


@pytest.mark.asyncio
async def test_matching_hash_without_exact_revision_is_not_materialized(
    documents, recorder
):
    await _conversation("conv-unfenced-summary")
    episode = await _episode(
        status="provisional",
        conversations=["conv-unfenced-summary"],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
    )
    episode.detailed_summary = "Summary without an exact revision fence"
    episode.detailed_summary_scope_hash = episode_summary_scope_hash(episode)
    episode.detailed_summary_revision = None
    await episode.save()

    outcome = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert outcome == {"summaries": [episode.episode_key], "events": []}
    assert recorder.summaries == [(episode.episode_id, episode.revision)]


@pytest.mark.asyncio
async def test_two_provisional_episodes_never_extract_or_summarize_raw_conversation(
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

    assert outcome == {"summaries": [], "events": []}
    assert recorder.memory == []
    assert recorder.summaries == []
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

    assert outcome == {"summaries": [], "events": []}
    assert recorder.memory == []
    assert await dispatch_ready_episodes() == {"unlatched": 0, "dispatched": 0}
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
async def test_recovery_scan_dispatches_only_unlatched_ready_episodes(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(status="settled", conversations=["conv-one"])

    first = await dispatch_ready_episodes()
    second = await dispatch_ready_episodes()

    assert first == {"unlatched": 1, "dispatched": 1}
    assert second == {"unlatched": 0, "dispatched": 0}
    assert recorder.summaries == [(episode.episode_id, 1)]
    latch = await EpisodeDispatchLatch.find_one(
        EpisodeDispatchLatch.episode_key == episode.episode_key,
        {"event_type": {"$regex": "^episode.detailed_summary:"}},
    )
    assert latch is not None


@pytest.mark.asyncio
async def test_uncommitted_snapshot_never_dispatches_completion(documents, recorder):
    await _conversation("conv-uncommitted")
    episode = await _episode(
        status="settled",
        conversations=["conv-uncommitted"],
        publish=False,
    )
    journal = await _publish_episode(
        episode,
        [episode.local_date],
        journal_status="snapshots_installed",
    )

    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [],
        "events": [],
    }
    assert recorder.summaries == []
    assert recorder.events == []
    assert await EpisodeDispatchLatch.find_all().count() == 0

    journal.status = "committed"
    journal.committed_at = START
    await journal.save()
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [episode.episode_key],
        "events": [episode.episode_key],
    }


@pytest.mark.asyncio
async def test_recovery_ignores_partially_inserted_orphan_revision(documents, recorder):
    await _conversation("conv-orphan")
    await _episode(
        status="settled",
        conversations=["conv-orphan"],
        publish=False,
    )

    assert await dispatch_ready_episodes() == {"unlatched": 0, "dispatched": 0}
    assert recorder.summaries == []
    assert recorder.events == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
async def test_cross_midnight_summary_waits_for_every_safe_current_day(
    documents, recorder
):
    await _conversation("conv-midnight")
    episode = await _episode(
        episode_key="cross-midnight",
        status="provisional",
        conversations=["conv-midnight"],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
        started_at=datetime(2026, 8, 15, 23, 50, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 16, 0, 10, tzinfo=timezone.utc),
        publish=False,
    )
    next_date = episode.local_date + timedelta(days=1)
    await _publish_episode(episode, [episode.local_date])

    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [],
        "events": [],
    }

    await _publish_episode(episode, [episode.local_date, next_date])
    next_day = await TimelineDay.find_one(
        TimelineDay.user_id == USER,
        TimelineDay.local_date == next_date,
        TimelineDay.timezone == episode.timezone,
    )
    assert next_day is not None
    next_day.snapshot_state = "dirty"
    await next_day.save()
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [],
        "events": [],
    }

    next_day.snapshot_state = "ready"
    await next_day.save()
    stale_revision = episode.model_copy(update={"revision": episode.revision + 1})
    await _publish_episode(stale_revision, [next_date])
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [],
        "events": [],
    }

    await _publish_episode(episode, [episode.local_date, next_date])
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [episode.episode_key],
        "events": [],
    }
    assert recorder.summaries == [(episode.episode_id, episode.revision)]


@pytest.mark.asyncio
async def test_recovery_scan_includes_structurally_confirmed_provisional_revision(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(
        status="provisional",
        conversations=["conv-one"],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
    )

    first = await dispatch_ready_episodes()
    second = await dispatch_ready_episodes()

    assert first == {"unlatched": 1, "dispatched": 1}
    assert second == {"unlatched": 0, "dispatched": 0}
    assert recorder.summaries == [(episode.episode_id, episode.revision)]
    assert recorder.events == []


@pytest.mark.asyncio
async def test_recovery_reclaims_an_expired_summary_process_lease(documents, recorder):
    await _conversation("conv-crashed-worker")
    episode = await _episode(
        status="provisional",
        conversations=["conv-crashed-worker"],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
    )

    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [episode.episode_key],
        "events": [],
    }
    assert await dispatch_ready_episodes() == {"unlatched": 0, "dispatched": 0}
    await EpisodeDispatchLatch.get_pymongo_collection().update_one(
        {"episode_key": episode.episode_key},
        {"$set": {"dispatched_at": START - timedelta(hours=1)}},
    )

    assert await dispatch_ready_episodes() == {"unlatched": 1, "dispatched": 1}
    assert recorder.summaries == [
        (episode.episode_id, episode.revision),
        (episode.episode_id, episode.revision),
    ]


@pytest.mark.asyncio
async def test_recovery_selects_due_work_after_completed_journal_history(
    documents, recorder, monkeypatch
):
    confirmed = ["started_at", "ended_at", "evidence_refs"]
    for index in range(8):
        conversation = await _conversation(f"conv-complete-{index}")
        episode = await _episode(
            episode_key=f"complete-{index}",
            status="provisional",
            conversations=[conversation.conversation_id],
            confirmed_fields=confirmed,
            dispatch_pending=False,
        )
        episode.ended_at = START + timedelta(minutes=20 + index)
        episode.detailed_summary = f"Completed account {index}"
        episode.detailed_summary_scope_hash = episode_summary_scope_hash(episode)
        episode.detailed_summary_revision = episode.revision
        episode.detailed_summary_generated_at = START
        await episode.save()

    due_conversation = await _conversation("conv-due-after-history")
    due = await _episode(
        episode_key="due-after-history",
        status="provisional",
        conversations=[due_conversation.conversation_id],
        confirmed_fields=confirmed,
    )
    due.ended_at = START + timedelta(minutes=40)
    await due.save()

    publication_checks: list[str] = []
    check_publication = dispatch_module.episode_revision_is_published

    async def counted_publication_check(episode):
        publication_checks.append(episode.episode_key)
        return await check_publication(episode)

    monkeypatch.setattr(
        dispatch_module,
        "episode_revision_is_published",
        counted_publication_check,
    )

    recovered = await dispatch_ready_episodes(limit=2)

    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert recorder.summaries == [(due.episode_id, due.revision)]
    assert publication_checks
    assert due.episode_key in publication_checks


@pytest.mark.asyncio
async def test_recovery_never_inspects_completed_publication_history(
    documents, recorder, monkeypatch
):
    await _conversation("conv-completed-publication")
    await _episode(
        episode_key="completed-publication",
        status="provisional",
        conversations=["conv-completed-publication"],
        confirmed_fields=["started_at", "ended_at", "evidence_refs"],
        dispatch_pending=False,
    )
    publication_checks: list[str] = []
    check_publication = dispatch_module.episode_revision_is_published

    async def counted_publication_check(episode):
        publication_checks.append(episode.episode_key)
        return await check_publication(episode)

    monkeypatch.setattr(
        dispatch_module,
        "episode_revision_is_published",
        counted_publication_check,
    )

    assert await dispatch_ready_episodes(limit=1) == {
        "unlatched": 0,
        "dispatched": 0,
    }
    assert publication_checks == []
    assert recorder.summaries == []


@pytest.mark.asyncio
async def test_due_publication_recovery_closes_idempotently(documents, recorder):
    await _conversation("conv-pending-publication")
    episode = await _episode(
        episode_key="pending-publication",
        status="settled",
        conversations=["conv-pending-publication"],
    )

    assert await dispatch_ready_episodes(limit=1) == {
        "unlatched": 1,
        "dispatched": 1,
    }
    journal = await TimelinePublicationJournal.find_one(
        TimelinePublicationJournal.dispatch_pending == True  # noqa: E712
    )
    assert journal is not None
    assert journal.dispatch_completed_at is None
    assert recorder.summaries == [(episode.episode_id, episode.revision)]
    assert len(recorder.events) == 1

    episode.detailed_summary = "The exact detailed account"
    episode.detailed_summary_scope_hash = episode_summary_scope_hash(episode)
    episode.detailed_summary_revision = episode.revision
    episode.detailed_summary_generated_at = START
    await episode.save()
    assert await dispatch_ready_episodes(limit=1) == {
        "unlatched": 0,
        "dispatched": 0,
    }
    journal = await TimelinePublicationJournal.get(journal.id)
    assert journal is not None
    assert journal.dispatch_pending is False
    assert journal.dispatch_completed_at is not None
    assert await dispatch_ready_episodes(limit=1) == {
        "unlatched": 0,
        "dispatched": 0,
    }
    assert recorder.summaries == [(episode.episode_id, episode.revision)]
    assert len(recorder.events) == 1


@pytest.mark.asyncio
async def test_recovery_crash_leaves_publication_pending(
    documents, recorder, monkeypatch
):
    await _conversation("conv-crashed-recovery")
    await _episode(
        episode_key="crashed-recovery",
        status="settled",
        conversations=["conv-crashed-recovery"],
    )

    async def crash_before_dispatch(_user_id, _episode_ids):
        raise RuntimeError("worker stopped during recovery")

    monkeypatch.setattr(
        dispatch_module,
        "dispatch_classified_episodes",
        crash_before_dispatch,
    )
    with pytest.raises(RuntimeError, match="worker stopped during recovery"):
        await dispatch_ready_episodes(limit=1)

    journal = await TimelinePublicationJournal.find_one(
        TimelinePublicationJournal.dispatch_pending == True  # noqa: E712
    )
    assert journal is not None
    assert journal.dispatch_completed_at is None


@pytest.mark.asyncio
async def test_recovery_uses_authoritative_audio_range_conversation_ids(
    documents, recorder
):
    """A missing lineage hint must not hide an episode's canonical audio owner."""

    await _conversation("conv-from-audio-range")
    await _episode(
        status="settled",
        conversations=[],
        audio_conversations=["conv-from-audio-range"],
    )

    recovered = await dispatch_ready_episodes()

    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert recorder.summaries
    assert await EpisodeDispatchLatch.find_one(
        EpisodeDispatchLatch.episode_key == "key-one",
        {"event_type": {"$regex": "^episode.detailed_summary:"}},
    )


@pytest.mark.asyncio
async def test_recovery_scan_repairs_missing_completion_latch_without_repeating_summary(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(status="settled", conversations=["conv-one"])
    await dispatch_module.enqueue_episode_detailed_summary(episode)

    recovered = await dispatch_ready_episodes()

    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert len(recorder.summaries) == 1
    assert len(recorder.events) == 1
    assert await EpisodeDispatchLatch.find_all().count() == 2


@pytest.mark.asyncio
async def test_recovery_scan_does_not_hydrate_terminal_completion_latches(
    documents, recorder
):
    await _conversation("conv-one")
    episode = await _episode(status="settled", conversations=["conv-one"])
    await EpisodeDispatchLatch.get_pymongo_collection().insert_one(
        {
            "user_id": USER,
            "episode_key": episode.episode_key,
            "event_type": "conversation.complete",
            "episode_id": episode.episode_id,
            "revision": episode.revision,
            "dispatched_at": START,
        }
    )

    recovered = await dispatch_ready_episodes()

    assert recovered == {"unlatched": 1, "dispatched": 1}
    assert recorder.summaries == [(episode.episode_id, episode.revision)]
    assert recorder.events == []


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

    assert dispatched == {"summaries": ["key-one"], "events": []}
    assert len(recorder.events) == 1


@pytest.mark.asyncio
async def test_media_classified_episode_dispatches_nothing(documents, recorder):
    episode = await _episode(conversational=False, conversations=["conv-one"])

    dispatched = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert dispatched == {"summaries": [], "events": []}
    assert recorder.events == []
    assert recorder.memory == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "superseded"])
async def test_unready_episodes_never_dispatch(documents, recorder, status):
    episode = await _episode(status=status, conversations=["conv-one"])

    dispatched = await dispatch_classified_episodes(USER, [episode.episode_id])

    assert dispatched == {"summaries": [], "events": []}
    assert recorder.events == []
    assert await EpisodeDispatchLatch.find_all().count() == 0


@pytest.mark.asyncio
async def test_dispatch_failure_releases_the_latch_and_a_retry_fires(
    documents, monkeypatch
):
    await _conversation("conv-one")
    episode = await _episode(conversations=["conv-one"])

    failing = _Recorder(fail=True)
    monkeypatch.setattr(dispatch_module, "_fire_plugins", _fire_plugins_with(failing))
    monkeypatch.setattr(
        dispatch_module,
        "_enqueue_episode_summary_job",
        _enqueue_episode_summary_job_with(failing),
    )
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": ["key-one"],
        "events": [],
    }
    # The summary stays latched; only the failed plugin latch rolls back.
    assert await EpisodeDispatchLatch.find_all().count() == 1

    healthy = _Recorder()
    monkeypatch.setattr(dispatch_module, "_fire_plugins", _fire_plugins_with(healthy))
    assert await dispatch_classified_episodes(USER, [episode.episode_id]) == {
        "summaries": [],
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


def _close_path_fakes(monkeypatch):
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
        queue_controller,
        "enqueue_summary_job_bundle",
        lambda *args, **kwargs: {
            stage: SimpleNamespace(id=stage)
            for stage in ("title", "short_summary", "detailed_summary")
        },
    )
    return queue_controller, queues


def test_close_path_skips_memory_and_plugin_dispatch(monkeypatch):
    queue_controller, queues = _close_path_fakes(monkeypatch)

    jobs = queue_controller.start_post_conversation_jobs(
        "conv-one", "64b64c3f2f6e4a1f9c123456"
    )

    assert jobs["memory"] is None
    assert queues["memory"].enqueued == []
    assert jobs["title"] is None
    assert jobs["short_summary"] is None
    assert jobs["detailed_summary"] is None
    # The terminal job still runs — it owns end_reason/completed_at/status — but it is
    # the finalize-only variant that dispatches no user-facing event.
    assert [name for name, _ in queues["default"].enqueued] == [
        "finalize_conversation_close_job"
    ]
    # Evidence producers are untouched.
    assert [name for name, _ in queues["transcription"].enqueued] == [
        "recognise_speakers_job"
    ]


def test_space_close_path_waits_for_scoped_review(monkeypatch):
    """A deliberate space has its own review gate instead of Timeline settlement."""

    queue_controller, queues = _close_path_fakes(monkeypatch)

    jobs = queue_controller.start_post_conversation_jobs(
        "space-conversation",
        "user-one",
        memory_space_id="5a265801-b8ca-4667-ae7d-07b2c170ecad",
    )

    assert jobs["memory"] is None
    assert queues["memory"].enqueued == []
    assert [name for name, _ in queues["default"].enqueued] == [
        "dispatch_conversation_complete_event_job"
    ]
