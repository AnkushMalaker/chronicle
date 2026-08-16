"""Tests for reclaiming raw-audio write-ahead logs after a recording ends.

Two failures put 764 MB of undeletable streams in Redis on a live deployment:

1. A stream whose session hash was gone read as "not terminal", so every cleanup
   path refused it forever. Absence of a hash means the session is *over*, not that
   it might still be running.
2. Deletion was only ever attempted at the end of a recording. Six streams that
   were fully drained and safe to delete sat for up to 31 hours because nothing
   asked a second time.

What must not regress is the fail-closed rule: a stream is deleted only when Redis
proves every registered consumer group has zero pending and zero lag.
"""

import pytest
from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.services.audio_stream import reclaim as reclaim_module
from advanced_omi_backend.services.audio_stream.durability import (
    AUDIO_PERSISTENCE_GROUP,
    delete_stream_if_durable,
    session_append_closed,
)
from advanced_omi_backend.services.audio_stream.reclaim import (
    reclaim_settled_audio_streams,
)
from advanced_omi_backend.services.audio_stream.session_store import SessionStatus
from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStore as ProductionSessionStore,
)

pytestmark = pytest.mark.unit


class SessionStore(ProductionSessionStore):
    """Ambient provenance fixture for WAL-reclaim tests."""

    async def init_session(self, session_id: str, **kwargs) -> None:
        kwargs.update(
            capture_epoch=0,
            processing_profile="ambient",
            effects={
                "aec": {"reporting": "unreported"},
                "noise_suppression": {"reporting": "unreported"},
            },
            voice_session_id=None,
        )
        await super().init_session(session_id, **kwargs)


async def _session(redis, session_id, status=None):
    store = SessionStore(redis)
    await store.init_session(
        session_id,
        user_id="u1",
        client_id="dev-phone",
        stream_name=f"audio:stream:{session_id}",
    )
    if status is SessionStatus.FINISHED:
        await store.mark_complete(session_id, "websocket_disconnect")
    return store


async def _drained_stream(redis, session_id, *, groups=(AUDIO_PERSISTENCE_GROUP,)):
    """A stream every group has read and acknowledged to the end."""
    stream = f"audio:stream:{session_id}"
    entry_id = await redis.xadd(stream, {"audio": b"x"})
    for group in groups:
        await redis.xgroup_create(stream, group, id="0")
        await redis.xreadgroup(group, "c1", {stream: ">"}, count=10)
        await redis.xack(stream, group, entry_id)
    return stream


# --------------------------------------------------------------------------- #
# When may the question be asked
# --------------------------------------------------------------------------- #


async def test_missing_session_hash_means_the_recording_is_over():
    """The orphan case: no hash, so nothing can append, so cleanup may proceed."""
    redis = fake_aioredis.FakeRedis()

    assert await session_append_closed(redis, "gone-session") is True


async def test_finished_session_is_closed_for_appends():
    redis = fake_aioredis.FakeRedis()
    await _session(redis, "s1", status=SessionStatus.FINISHED)

    assert await session_append_closed(redis, "s1") is True


async def test_active_session_is_not_closed_for_appends():
    redis = fake_aioredis.FakeRedis()
    await _session(redis, "s1")

    assert await session_append_closed(redis, "s1") is False


async def test_hash_present_but_status_unset_stays_retained():
    """A session resurrected with partial fields is ambiguous, so retain it."""
    redis = fake_aioredis.FakeRedis()
    await redis.hset("audio:session:s1", "stream_name", "audio:stream:s1")

    assert await session_append_closed(redis, "s1") is False


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_redis(monkeypatch):
    redis = fake_aioredis.FakeRedis()
    monkeypatch.setattr(reclaim_module, "create_async_redis", lambda: redis)
    monkeypatch.setattr(redis, "aclose", lambda: _noop())
    return redis


async def _noop():
    return None


async def test_orphaned_drained_stream_is_reclaimed(fake_redis):
    stream = await _drained_stream(fake_redis, "orphan-1")  # no session hash at all

    result = await reclaim_settled_audio_streams()

    assert result["reclaimed"] == 1
    assert await fake_redis.exists(stream) == 0


async def test_finished_session_stream_is_reclaimed(fake_redis):
    await _session(fake_redis, "s1", status=SessionStatus.FINISHED)
    stream = await _drained_stream(fake_redis, "s1")

    result = await reclaim_settled_audio_streams()

    assert result["reclaimed"] == 1
    assert await fake_redis.exists(stream) == 0


async def test_live_session_stream_is_never_touched(fake_redis):
    await _session(fake_redis, "s1")
    stream = await _drained_stream(fake_redis, "s1")

    result = await reclaim_settled_audio_streams()

    assert result["reclaimed"] == 0
    assert await fake_redis.exists(stream) == 1


async def test_unacknowledged_audio_is_never_deleted(fake_redis):
    """The fail-closed rule: pending entries mean the audio may not be in Mongo."""
    await _session(fake_redis, "s1", status=SessionStatus.FINISHED)
    stream = "audio:stream:s1"
    await fake_redis.xadd(stream, {"audio": b"x"})
    await fake_redis.xgroup_create(stream, AUDIO_PERSISTENCE_GROUP, id="0")
    # Read without acknowledging — the entry stays pending.
    await fake_redis.xreadgroup(AUDIO_PERSISTENCE_GROUP, "c1", {stream: ">"}, count=10)

    result = await reclaim_settled_audio_streams()

    assert result["reclaimed"] == 0
    assert await fake_redis.exists(stream) == 1


class _StubRedis:
    """A client returning one chosen XINFO GROUPS payload.

    The lag rule cannot be exercised through fakeredis: it reports ``lag: 0`` for a
    group that has read nothing, where real Redis reports the true backlog. Driving
    ``inspect_stream_retention`` directly tests the production rule instead of the
    fake's approximation of Redis.
    """

    def __init__(self, groups):
        self._groups = groups
        self.deleted = False

    async def exists(self, key):
        return 1

    async def execute_command(self, *args):
        return self._groups

    async def delete(self, key):
        self.deleted = True


def _xinfo_group(name, *, pending, lag):
    return [b"name", name.encode(), b"pending", pending, b"lag", lag]


async def test_unread_audio_is_never_deleted():
    """Lag, rather than pending: a group that never caught up blocks deletion."""
    redis = _StubRedis([_xinfo_group(AUDIO_PERSISTENCE_GROUP, pending=0, lag=1)])

    decision = await delete_stream_if_durable(
        redis, "audio:stream:s1", required_groups={AUDIO_PERSISTENCE_GROUP}
    )

    assert decision.safe_to_delete is False
    assert decision.reason.startswith("consumer_backlog")
    assert redis.deleted is False


async def test_unknown_lag_is_never_treated_as_drained():
    """Redis reports lag=None when it cannot compute it. Unknown is not proof."""
    redis = _StubRedis([_xinfo_group(AUDIO_PERSISTENCE_GROUP, pending=0, lag=None)])

    decision = await delete_stream_if_durable(
        redis, "audio:stream:s1", required_groups={AUDIO_PERSISTENCE_GROUP}
    )

    assert decision.safe_to_delete is False
    assert redis.deleted is False


async def test_an_optional_group_with_backlog_also_blocks_deletion():
    """Every registered group counts, not only the required ones."""
    redis = _StubRedis(
        [
            _xinfo_group(AUDIO_PERSISTENCE_GROUP, pending=0, lag=0),
            _xinfo_group("wakeword_detection", pending=3, lag=0),
        ]
    )

    decision = await delete_stream_if_durable(
        redis, "audio:stream:s1", required_groups={AUDIO_PERSISTENCE_GROUP}
    )

    assert decision.safe_to_delete is False
    assert redis.deleted is False


async def test_a_stream_with_no_persistence_group_is_never_deleted(fake_redis):
    """A required group that was never created is missing evidence, not proof."""
    await _session(fake_redis, "s1", status=SessionStatus.FINISHED)
    stream = await _drained_stream(fake_redis, "s1", groups=("wakeword_detection",))

    result = await reclaim_settled_audio_streams()

    assert result["reclaimed"] == 0
    assert await fake_redis.exists(stream) == 1


async def test_sweep_is_bounded_per_tick(fake_redis):
    for i in range(5):
        await _drained_stream(fake_redis, f"orphan-{i}")

    result = await reclaim_settled_audio_streams(max_streams=2)

    assert result["reclaimed"] == 2
    remaining = await fake_redis.keys("audio:stream:*")
    assert len(remaining) == 3


# --------------------------------------------------------------------------- #
# Consumer hygiene (folded in from the old "Cleanup Stuck Workers" button)
# --------------------------------------------------------------------------- #


class _ConsumerStubRedis:
    """Serves chosen XINFO GROUPS / CONSUMERS payloads and records DELCONSUMER.

    A consumer's idle time cannot be fast-forwarded in fakeredis, so the threshold
    is driven directly rather than by waiting five minutes.
    """

    def __init__(self, consumers):
        self._consumers = consumers
        self.deleted: list = []

    async def execute_command(self, *args):
        if args[0] == "XINFO" and args[1] == "GROUPS":
            return [
                [b"name", AUDIO_PERSISTENCE_GROUP.encode(), b"pending", 0, b"lag", 0]
            ]
        if args[0] == "XINFO" and args[1] == "CONSUMERS":
            return [
                [b"name", n.encode(), b"pending", p, b"idle", i]
                for n, p, i in self._consumers
            ]
        if args[0] == "XGROUP" and args[1] == "DELCONSUMER":
            self.deleted.append(args[4])
            return 0
        raise AssertionError(f"unexpected command {args}")


async def test_a_consumer_idle_with_nothing_pending_is_dropped():
    redis = _ConsumerStubRedis([("ghost", 0, 600_000)])

    dropped = await reclaim_module._drop_finished_consumers(redis, "audio:stream:s1")

    assert dropped == 1
    assert redis.deleted == ["ghost"]


async def test_a_recently_active_consumer_is_left_alone():
    redis = _ConsumerStubRedis([("live", 0, 800)])

    dropped = await reclaim_module._drop_finished_consumers(redis, "audio:stream:s1")

    assert dropped == 0
    assert redis.deleted == []


async def test_an_idle_consumer_still_holding_messages_is_left_alone():
    """However idle, a consumer with a backlog owns those messages."""
    redis = _ConsumerStubRedis([("stalled", 119, 21_000_000)])

    dropped = await reclaim_module._drop_finished_consumers(redis, "audio:stream:s1")

    assert dropped == 0
    assert redis.deleted == []


async def test_a_consumer_holding_pending_entries_is_never_dropped(fake_redis):
    """The rule the old button's name got backwards.

    Only the consumer that committed the side effect may acknowledge its messages,
    so a consumer with a backlog is reported, never cleared.
    """
    await _session(fake_redis, "s1", status=SessionStatus.FINISHED)
    stream = "audio:stream:s1"
    await fake_redis.xadd(stream, {"audio": b"x"})
    await fake_redis.xgroup_create(stream, AUDIO_PERSISTENCE_GROUP, id="0")
    await fake_redis.xreadgroup(
        AUDIO_PERSISTENCE_GROUP, "busy", {stream: ">"}, count=10
    )

    result = await reclaim_settled_audio_streams()

    assert result["dropped_consumers"] == 0
    assert result["blocked"]  # surfaced as an incident rather than force-cleared
    consumers = await fake_redis.execute_command(
        "XINFO", "CONSUMERS", stream, AUDIO_PERSISTENCE_GROUP
    )
    assert len(consumers) == 1
