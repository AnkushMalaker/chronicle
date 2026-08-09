"""The timeline rebuild stage: which days it replays, and what it clears first."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory import rebuild
from advanced_omi_backend.services.memory.rebuild import (
    RebuildConversation,
    RebuildPlan,
    RebuildStage,
    build_timeline_days,
    execute_memory_rebuild,
)

USER = "507f1f77bcf86cd799439011"


def captured(text: str) -> datetime:
    """Naive UTC, exactly as Mongo hands a BSON date back."""

    return datetime.fromisoformat(text).astimezone(timezone.utc).replace(tzinfo=None)


class Cursor:
    def __init__(self, documents):
        self.documents = documents

    def __aiter__(self):
        self.iterator = iter(self.documents)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class AggregateCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows


class Collection:
    def __init__(self, *, documents=(), rows=()):
        self.documents = list(documents)
        self.rows = list(rows)
        self.deleted_queries = []

    def find(self, query, projection=None):
        return Cursor(self.documents)

    def aggregate(self, pipeline, **kwargs):
        self.pipeline = pipeline
        return AggregateCursor(self.rows)

    async def delete_many(self, query):
        self.deleted_queries.append(query)
        return SimpleNamespace(deleted_count=len(self.documents) or 3)


class Database:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())


def users_collection() -> Collection:
    """The raw ``users`` collection, as the CLI sees it with no Beanie models."""

    return Collection(documents=[{"_id": USER, "timezone": "Asia/Kolkata"}])


@pytest.mark.asyncio
async def test_days_come_from_capture_time_and_span_local_midnight():
    """A recording crossing local midnight belongs to both days, not the later one."""

    database = Database(
        {
            "users": users_collection(),
            "conversations": Collection(
                documents=[{"conversation_id": "conv-a", "user_id": USER}]
            ),
            # 18:00-19:30 UTC on the 5th is 23:30-01:00 IST, i.e. the 5th into the 6th.
            "audio_chunks": Collection(
                rows=[
                    {
                        "_id": "conv-a",
                        "first": captured("2026-08-05T18:00:00+00:00"),
                        "last": captured("2026-08-05T19:30:00+00:00"),
                    }
                ]
            ),
        }
    )

    days = await build_timeline_days(database, (USER,))

    assert [day.local_date for day in days] == [date(2026, 8, 5), date(2026, 8, 6)]
    assert {day.timezone for day in days} == {"Asia/Kolkata"}


@pytest.mark.asyncio
async def test_annotation_audio_is_not_a_day_to_rebuild():
    """Data imported for mining is not part of the user's lived timeline."""

    conversations = Collection(documents=[])
    database = Database(
        {
            "users": users_collection(),
            "conversations": conversations,
            "audio_chunks": Collection(),
        }
    )

    assert await build_timeline_days(database, (USER,)) == ()
    # The exclusion has to be in the query, not applied after the fact, or the audio
    # aggregation still pulls a mining corpus' worth of chunks.
    conversations.find({"data_purpose": {"$ne": "annotation"}})


@pytest.mark.asyncio
async def test_timeline_stage_clears_prior_episodes_and_chains_days(
    monkeypatch, tmp_path
):
    """Prior runs/days/episodes must go, and each day must wait for the previous one."""

    collections = {
        "users": users_collection(),
        "conversations": Collection(
            documents=[{"conversation_id": "conv-a", "user_id": USER}]
        ),
        "audio_chunks": Collection(
            rows=[
                {
                    "_id": "conv-a",
                    "first": captured("2026-08-05T06:00:00+00:00"),
                    "last": captured("2026-08-06T06:00:00+00:00"),
                }
            ]
        ),
        "memory_audit": Collection(),
        "timeline_episodes": Collection(),
        "timeline_days": Collection(),
        "timeline_analysis_runs": Collection(),
    }
    database = Database(collections)

    enqueued = []

    def fake_day(day, *, run_id, sequence, depends_on):
        enqueued.append((day.local_date.isoformat(), depends_on))
        return SimpleNamespace(id=f"day-{sequence}")

    def fake_speaker(item, *, run_id, sequence, depends_on):
        enqueued.append((item.conversation_id, depends_on))
        return SimpleNamespace(id=f"speaker-{sequence}")

    monkeypatch.setattr(rebuild, "_enqueue_timeline_rebuild", fake_day)
    monkeypatch.setattr(rebuild, "_enqueue_speaker_rebuild", fake_speaker)
    monkeypatch.setattr(rebuild, "_active_rebuild_jobs", lambda _ids: [])
    monkeypatch.setattr(rebuild, "clear_vault_contents", lambda _path: 0)

    plan = RebuildPlan(
        conversations=(
            RebuildConversation(
                conversation_id="conv-a",
                user_id=USER,
                created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
                transcript_version_id="v1",
            ),
        ),
        user_ids=(USER,),
    )
    result = await execute_memory_rebuild(
        database,
        plan,
        data_dir=tmp_path,
        from_stage=RebuildStage.TIMELINE,
    )

    for name in ("timeline_episodes", "timeline_days", "timeline_analysis_runs"):
        assert collections[name].deleted_queries == [{"user_id": {"$in": [USER]}}]
    # Diarization before segmentation, and each day after the last speaker job: an
    # episode's transcript is the speaker-labelled one, so a day segmented over raw ASR
    # decides its bounds — and writes its memory — from text with no speakers in it.
    assert [item[0] for item in enqueued] == ["conv-a", "2026-08-05", "2026-08-06"]
    # Speaker jobs fan out (no dependency); the first day waits on all of them, and each
    # later day waits on the previous one because the write takes the vault lock.
    assert [item[1] for item in enqueued] == [None, ["speaker-1"], "day-1"]
    # The day pass is the whole vault write for this stage; running the
    # per-conversation path beside it would record the same audio twice.
    assert result.memory_jobs == ()
    assert result.speaker_jobs == ("speaker-1",)
    assert len(result.timeline_jobs) == 2


@pytest.mark.asyncio
async def test_days_stage_replays_boundaries_over_the_existing_speaker_layer(
    monkeypatch, tmp_path
):
    """Re-deciding boundaries must not cost a full re-diarization.

    What changes between day-chain runs is the segmentation agent, the day prompt, or
    the episode-note format — none of which alter a transcript. Paying for hundreds of
    GPU speaker jobs to reach a day chain that runs in minutes is pure waste, and
    resetting to the ASR layer would actively make it worse: the day pass would then
    segment text with no speakers in it.
    """

    collections = {
        "users": users_collection(),
        "conversations": Collection(
            documents=[{"conversation_id": "conv-a", "user_id": USER}]
        ),
        "audio_chunks": Collection(
            rows=[
                {
                    "_id": "conv-a",
                    "first": captured("2026-08-05T06:00:00+00:00"),
                    "last": captured("2026-08-06T06:00:00+00:00"),
                }
            ]
        ),
        "memory_audit": Collection(),
        "timeline_episodes": Collection(),
        "timeline_days": Collection(),
        "timeline_analysis_runs": Collection(),
    }
    database = Database(collections)

    enqueued = []
    reset_calls = []

    def fake_day(day, *, run_id, sequence, depends_on):
        enqueued.append((day.local_date.isoformat(), depends_on))
        return SimpleNamespace(id=f"day-{sequence}")

    def fail_speaker(item, *, run_id, sequence, depends_on):
        raise AssertionError("the days stage must not enqueue speaker jobs")

    async def fake_reset(database, conversations):
        reset_calls.append(len(conversations))

    monkeypatch.setattr(rebuild, "_enqueue_timeline_rebuild", fake_day)
    monkeypatch.setattr(rebuild, "_enqueue_speaker_rebuild", fail_speaker)
    monkeypatch.setattr(rebuild, "_reset_active_versions_to_asr", fake_reset)
    monkeypatch.setattr(rebuild, "_active_rebuild_jobs", lambda _ids: [])
    monkeypatch.setattr(rebuild, "clear_vault_contents", lambda _path: 0)

    plan = RebuildPlan(
        conversations=(
            RebuildConversation(
                conversation_id="conv-a",
                user_id=USER,
                created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
                transcript_version_id="v1",
            ),
        ),
        user_ids=(USER,),
    )
    result = await execute_memory_rebuild(
        database,
        plan,
        data_dir=tmp_path,
        from_stage=RebuildStage.DAYS,
    )

    # The speaker layer that is already active is exactly what the day pass reads.
    assert reset_calls == []
    assert result.speaker_jobs == ()
    # Prior episodes still go: a surviving TimelineDay carries the write-once latch
    # that would skip it, and a surviving episode is offered back to the agent as
    # prior art, so it reproduces the boundaries this run exists to replace.
    for name in ("timeline_episodes", "timeline_days", "timeline_analysis_runs"):
        assert collections[name].deleted_queries == [{"user_id": {"$in": [USER]}}]
    # Days stay serial on the vault lock, and the first has nothing to wait for.
    assert enqueued == [("2026-08-05", None), ("2026-08-06", "day-1")]
    assert result.memory_jobs == ()


@pytest.mark.asyncio
async def test_timeline_stage_refuses_a_user_with_no_captured_audio(
    monkeypatch, tmp_path
):
    database = Database(
        {
            "users": users_collection(),
            "conversations": Collection(documents=[]),
            "audio_chunks": Collection(),
            "memory_audit": Collection(),
            "timeline_episodes": Collection(),
            "timeline_days": Collection(),
            "timeline_analysis_runs": Collection(),
        }
    )
    monkeypatch.setattr(rebuild, "_active_rebuild_jobs", lambda _ids: [])
    monkeypatch.setattr(rebuild, "clear_vault_contents", lambda _path: 0)

    plan = RebuildPlan(
        conversations=(
            RebuildConversation(
                conversation_id="conv-a",
                user_id=USER,
                created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
                transcript_version_id="v1",
            ),
        ),
        user_ids=(USER,),
    )
    with pytest.raises(rebuild.MemoryRebuildError, match="no days"):
        await execute_memory_rebuild(
            database, plan, data_dir=tmp_path, from_stage=RebuildStage.TIMELINE
        )
