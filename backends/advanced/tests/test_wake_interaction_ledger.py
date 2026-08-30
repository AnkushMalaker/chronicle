from datetime import datetime, timezone

import pytest

from advanced_omi_backend.services.wakeword.interaction_ledger import (
    WakeAudioInterval,
    WakeInteractionFact,
    WakeInteractionLedger,
    WakeInteractionLedgerConflict,
)


class _InsertResult:
    def __init__(self, upserted_id):
        self.upserted_id = upserted_id


class FakeCollection:
    """Minimal adapter at the ledger's public persistence seam."""

    def __init__(self):
        self.rows = {}

    async def update_one(self, identity, update, *, upsert):
        assert upsert is True
        key = (identity["wake_trace_id"], identity["stage"], identity["ordinal"])
        if key in self.rows:
            return _InsertResult(None)
        self.rows[key] = update["$setOnInsert"]
        return _InsertResult("new")

    async def find_one(self, identity):
        key = (identity["wake_trace_id"], identity["stage"], identity["ordinal"])
        return self.rows.get(key)


def fact(*, score=0.94):
    return WakeInteractionFact(
        wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
        stage="armed",
        ordinal=0,
        occurred_at=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
        user_id="user-1",
        client_id="user-1-phone",
        audio_session_id="session-1",
        capture_epoch=3,
        wakeword="hermes",
        audio_interval=WakeAudioInterval(
            start_ms=1250.0,
            end_ms=3250.0,
            started_at=datetime(2026, 8, 27, 8, 29, 58, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
        ),
        payload={"score": score},
    )


@pytest.mark.asyncio
async def test_append_is_idempotent_for_the_same_immutable_fact():
    collection = FakeCollection()
    ledger = WakeInteractionLedger(collection)

    first = await ledger.append(fact())
    replay = await ledger.append(fact())

    assert first.inserted is True
    assert replay.inserted is False
    assert len(collection.rows) == 1


@pytest.mark.asyncio
async def test_append_rejects_conflicting_reuse_of_a_stage_identity():
    collection = FakeCollection()
    ledger = WakeInteractionLedger(collection)
    await ledger.append(fact())

    with pytest.raises(WakeInteractionLedgerConflict):
        await ledger.append(fact(score=0.12))


def test_fact_requires_utc_and_an_ordered_absolute_audio_interval():
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        WakeInteractionFact(
            wake_trace_id="7ce4d46b-232f-47f9-8148-d595ed344cf2",
            stage="armed",
            ordinal=0,
            occurred_at=datetime(2026, 8, 27, 8, 30),
            user_id="user-1",
            client_id="user-1-phone",
            audio_session_id="session-1",
            capture_epoch=0,
        )

    with pytest.raises(ValueError, match="absolute bounds"):
        WakeAudioInterval(
            start_ms=0,
            end_ms=100,
            started_at=datetime(2026, 8, 27, 8, 30, 1, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
        )
