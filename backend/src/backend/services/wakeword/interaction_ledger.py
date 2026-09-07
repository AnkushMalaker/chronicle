"""Durable immutable stage facts for one wake interaction trace."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol
from uuid import UUID

WakeInteractionStage = Literal[
    "armed",
    "end_of_turn",
    "blocked",
    "command_resolved",
    "dispatched",
    "acted",
    "response_queued",
    "response_ready",
    "response_offered",
    "response_playing",
    "response_done",
    "followup_opened",
    "failed",
]

_STAGES = {
    "armed",
    "end_of_turn",
    "blocked",
    "command_resolved",
    "dispatched",
    "acted",
    "response_queued",
    "response_ready",
    "response_offered",
    "response_playing",
    "response_done",
    "followup_opened",
    "failed",
}


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


@dataclass(frozen=True)
class WakeAudioInterval:
    """One audio interval in both session-native and absolute UTC coordinates."""

    start_ms: float
    end_ms: float
    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("audio interval must have ordered native bounds")
        _require_utc(self.started_at, "audio interval started_at")
        _require_utc(self.ended_at, "audio interval ended_at")
        if self.ended_at <= self.started_at:
            raise ValueError("audio interval must have ordered absolute bounds")


@dataclass(frozen=True)
class WakeInteractionFact:
    """An immutable fact in a causally-linked wake interaction."""

    wake_trace_id: str
    stage: WakeInteractionStage
    ordinal: int
    occurred_at: datetime
    user_id: str
    client_id: str
    audio_session_id: str
    capture_epoch: int
    wakeword: str | None = None
    audio_interval: WakeAudioInterval | None = None
    voice_session_id: str | None = None
    turn_id: str | None = None
    turn_revision: int = 0
    response_id: str | None = None
    conversation_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            UUID(self.wake_trace_id)
        except (TypeError, ValueError) as error:
            raise ValueError("wake_trace_id must be a UUID") from error
        if self.stage not in _STAGES:
            raise ValueError(f"unknown wake interaction stage: {self.stage}")
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        _require_utc(self.occurred_at, "occurred_at")
        if not self.user_id or not self.client_id or not self.audio_session_id:
            raise ValueError("user_id, client_id, and audio_session_id are required")
        if self.capture_epoch < 0 or self.turn_revision < 0:
            raise ValueError("capture_epoch and turn_revision must be non-negative")

    def to_document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WakeInteractionAppendResult:
    inserted: bool


class WakeInteractionLedgerConflict(RuntimeError):
    """A stage identity was reused with different immutable content."""


class _Collection(Protocol):
    async def update_one(
        self, identity: dict[str, Any], update: dict[str, Any], *, upsert: bool
    ): ...

    async def find_one(self, identity: dict[str, Any]): ...


class WakeInteractionLedger:
    """Append immutable facts idempotently behind one persistence interface."""

    def __init__(self, collection: _Collection):
        self._collection = collection

    async def append(self, fact: WakeInteractionFact) -> WakeInteractionAppendResult:
        identity = {
            "wake_trace_id": fact.wake_trace_id,
            "stage": fact.stage,
            "ordinal": fact.ordinal,
        }
        document = fact.to_document()
        result = await self._collection.update_one(
            identity, {"$setOnInsert": document}, upsert=True
        )
        if result.upserted_id is not None:
            return WakeInteractionAppendResult(inserted=True)
        existing = await self._collection.find_one(identity)
        if existing is None:
            raise WakeInteractionLedgerConflict(
                "wake interaction fact disappeared during idempotency check"
            )
        existing = {key: value for key, value in existing.items() if key != "_id"}
        if existing != document:
            raise WakeInteractionLedgerConflict(
                "wake interaction stage identity already has different content"
            )
        return WakeInteractionAppendResult(inserted=False)
