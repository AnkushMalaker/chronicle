"""Fail-closed state and retention rules for the raw-audio write-ahead log.

There is one durability path: producer process memory -> Redis Stream -> MongoDB.
Redis consumer-group acknowledgement is the commit marker for that path.  Nothing
in this module retries through a different store or treats age as proof of safety.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from advanced_omi_backend.services.audio_stream.session_store import (
    SessionStatus,
    SessionStore,
)

AUDIO_PERSISTENCE_GROUP = "audio_persistence"


class SessionPhase(str, Enum):
    """Whether the producer may still append audio for this session."""

    ACTIVE = "active"
    DRAINING = "draining"


class ReadPhase(str, Enum):
    """Which part of the persistence consumer group must be read next."""

    RECOVERING_PENDING = "recovering_pending"
    TAILING_NEW = "tailing_new"


class PersistenceOutcome(str, Enum):
    """Terminal outcome of one RQ persistence attempt."""

    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class PersistenceRuntimeState:
    """Small explicit state machine used by the Mongo persistence worker.

    A retry creates a new instance in ``RECOVERING_PENDING``.  Errors never advance
    Redis delivery state: unread entries remain lagging and delivered entries remain
    pending until the next attempt commits and acknowledges them.
    """

    session: SessionPhase = SessionPhase.ACTIVE
    reader: ReadPhase = ReadPhase.RECOVERING_PENDING
    outcome: PersistenceOutcome = PersistenceOutcome.RUNNING

    def pending_recovered(self) -> None:
        self._require_running()
        if self.reader is not ReadPhase.RECOVERING_PENDING:
            raise RuntimeError("pending recovery can only finish once")
        self.reader = ReadPhase.TAILING_NEW

    def begin_draining(self) -> None:
        self._require_running()
        self.session = SessionPhase.DRAINING

    def complete(self) -> None:
        self._require_running()
        if self.session is not SessionPhase.DRAINING:
            raise RuntimeError(
                "persistence cannot complete while the session is active"
            )
        if self.reader is not ReadPhase.TAILING_NEW:
            raise RuntimeError("persistence cannot complete before pending recovery")
        self.outcome = PersistenceOutcome.COMPLETE

    def fail(self) -> None:
        self._require_running()
        self.outcome = PersistenceOutcome.ERROR

    def _require_running(self) -> None:
        if self.outcome is not PersistenceOutcome.RUNNING:
            raise RuntimeError(f"persistence attempt is already {self.outcome.value}")


@dataclass(frozen=True)
class ConsumerGroupProgress:
    """Durability-relevant fields from ``XINFO GROUPS``."""

    name: str
    pending: int
    lag: int | None

    @property
    def drained(self) -> bool:
        # Redis reports lag=None when it cannot calculate it. Unknown is unsafe.
        return self.pending == 0 and self.lag == 0


@dataclass(frozen=True)
class StreamRetentionDecision:
    """Result of proving whether a raw audio stream can be deleted."""

    safe_to_delete: bool
    reason: str
    groups: Mapping[str, ConsumerGroupProgress]


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def parse_consumer_groups(
    raw_groups: Sequence[Sequence[object]],
) -> dict[str, ConsumerGroupProgress]:
    """Parse the RESP array returned by ``XINFO GROUPS``."""

    parsed: dict[str, ConsumerGroupProgress] = {}
    for raw_group in raw_groups:
        values = {
            _text(raw_group[index]): raw_group[index + 1]
            for index in range(0, len(raw_group), 2)
        }
        name = _text(values.get("name", ""))
        pending = int(values.get("pending", 0))
        raw_lag = values.get("lag")
        lag = None if raw_lag is None else int(raw_lag)
        parsed[name] = ConsumerGroupProgress(name=name, pending=pending, lag=lag)
    return parsed


async def inspect_stream_retention(
    redis_client,
    stream_name: str,
    *,
    required_groups: Iterable[str],
) -> StreamRetentionDecision:
    """Prove that every required and registered consumer group is fully drained.

    The function is intentionally conservative: a missing group, unknown lag, unread
    entry, or pending delivery all prevent deletion.  Stream age is not evidence.
    """

    if not await redis_client.exists(stream_name):
        return StreamRetentionDecision(False, "stream_missing", {})

    raw_groups = await redis_client.execute_command("XINFO", "GROUPS", stream_name)
    groups = parse_consumer_groups(raw_groups or [])
    required = set(required_groups)
    missing = required - groups.keys()
    if missing:
        return StreamRetentionDecision(
            False,
            f"required_groups_missing:{','.join(sorted(missing))}",
            groups,
        )

    blocked = [group for group in groups.values() if not group.drained]
    if blocked:
        details = ",".join(
            f"{group.name}(pending={group.pending},lag={group.lag})"
            for group in blocked
        )
        return StreamRetentionDecision(False, f"consumer_backlog:{details}", groups)

    return StreamRetentionDecision(True, "all_consumers_drained", groups)


async def delete_stream_if_durable(
    redis_client,
    stream_name: str,
    *,
    required_groups: Iterable[str],
) -> StreamRetentionDecision:
    """Delete ``stream_name`` only after :func:`inspect_stream_retention` proves it."""

    decision = await inspect_stream_retention(
        redis_client, stream_name, required_groups=required_groups
    )
    if decision.safe_to_delete:
        await redis_client.delete(stream_name)
    return decision


_APPEND_CLOSED_STATUSES = (SessionStatus.FINALIZING, SessionStatus.FINISHED)


async def session_append_closed(redis_client, session_id: str) -> bool:
    """Whether the producer can no longer append to this session's stream.

    Retention gates on this rather than on status alone because a *missing* session
    hash read as "not terminal" and so blocked deletion permanently. The producer
    appends only while the hash says ACTIVE, and a live session's hash is explicitly
    persisted — never given a TTL — so an absent hash means the session is gone, not
    that it might still be running. Treating absence as non-terminal stranded the
    write-ahead log of every session whose hash had been reclaimed: 5 of 11 streams
    on this deployment, holding 764 MB that nothing could ever free.

    This is not a weakening of the fail-closed rule. Nothing is deleted on the
    strength of this answer alone — :func:`inspect_stream_retention` still has to
    prove every registered group has zero pending and zero lag, and the persistence
    consumer acknowledges only after a journaled Mongo commit. This decides whether
    the question may be *asked*, not what the answer is.

    A hash that exists but carries no status stays retained: a session can be
    resurrected with partial fields, so that case is genuinely ambiguous.
    """
    status = await SessionStore(redis_client).get_status(session_id)
    if status in _APPEND_CLOSED_STATUSES:
        return True
    if status is None:
        return not await SessionStore(redis_client).exists(session_id)
    return False
