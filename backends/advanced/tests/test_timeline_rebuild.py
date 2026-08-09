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


def epoch_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


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


@pytest.fixture
def user_lookup(monkeypatch):
    """Stand in for the User document and its timezone."""

    class FakeUser:
        def __init__(self):
            self.id = USER
            self.timezone = "Asia/Kolkata"

    class FakeFind:
        async def to_list(self):
            return [FakeUser()]

    monkeypatch.setattr(
        "advanced_omi_backend.models.user.User.find",
        staticmethod(lambda *_args, **_kwargs: FakeFind()),
    )


@pytest.mark.asyncio
async def test_days_come_from_capture_time_and_span_local_midnight(user_lookup):
    """A recording crossing local midnight belongs to both days, not the later one."""

    database = Database(
        {
            "conversations": Collection(
                documents=[{"conversation_id": "conv-a", "user_id": USER}]
            ),
            # 18:00-19:30 UTC on the 5th is 23:30-01:00 IST, i.e. the 5th into the 6th.
            "audio_chunks": Collection(
                rows=[
                    {
                        "_id": "conv-a",
                        "first": epoch_ms("2026-08-05T18:00:00+00:00"),
                        "last": epoch_ms("2026-08-05T19:30:00+00:00"),
                    }
                ]
            ),
        }
    )

    days = await build_timeline_days(database, (USER,))

    assert [day.local_date for day in days] == [date(2026, 8, 5), date(2026, 8, 6)]
    assert {day.timezone for day in days} == {"Asia/Kolkata"}


@pytest.mark.asyncio
async def test_annotation_audio_is_not_a_day_to_rebuild(user_lookup):
    """Data imported for mining is not part of the user's lived timeline."""

    conversations = Collection(documents=[])
    database = Database({"conversations": conversations, "audio_chunks": Collection()})

    assert await build_timeline_days(database, (USER,)) == ()
    # The exclusion has to be in the query, not applied after the fact, or the audio
    # aggregation still pulls a mining corpus' worth of chunks.
    conversations.find({"data_purpose": {"$ne": "annotation"}})


@pytest.mark.asyncio
async def test_timeline_stage_clears_prior_episodes_and_chains_days(
    monkeypatch, tmp_path, user_lookup
):
    """Prior runs/days/episodes must go, and each day must wait for the previous one."""

    collections = {
        "conversations": Collection(
            documents=[{"conversation_id": "conv-a", "user_id": USER}]
        ),
        "audio_chunks": Collection(
            rows=[
                {
                    "_id": "conv-a",
                    "first": epoch_ms("2026-08-05T06:00:00+00:00"),
                    "last": epoch_ms("2026-08-06T06:00:00+00:00"),
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
    assert [item[1] for item in enqueued] == [None, "speaker-1", "day-1"]
    # The day pass is the whole vault write for this stage; running the
    # per-conversation path beside it would record the same audio twice.
    assert result.memory_jobs == ()
    assert result.speaker_jobs == ("speaker-1",)
    assert len(result.timeline_jobs) == 2


@pytest.mark.asyncio
async def test_timeline_stage_refuses_a_user_with_no_captured_audio(
    monkeypatch, tmp_path, user_lookup
):
    database = Database(
        {
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
