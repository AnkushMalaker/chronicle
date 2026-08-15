"""Executor-independent contracts for semantic timeline analysis."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field, model_validator

from advanced_omi_backend.models.timeline import EvidenceKind, EvidenceRole


class TimelineEvidenceItem(BaseModel):
    evidence_id: str
    kind: EvidenceKind
    source_id: Optional[str] = None
    source_item_id: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    role: EvidenceRole
    excerpt: Optional[str] = None
    content_hash: Optional[str] = None
    ephemeral: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    image_filename: Optional[str] = None


class TimelineCoverageWindow(BaseModel):
    window_id: str
    started_at: datetime
    ended_at: datetime
    evidence_ids: list[str] = Field(default_factory=list)


class TimelineEvidenceManifest(BaseModel):
    user_id: str
    local_date: date
    timezone: str
    started_at: datetime
    ended_at: datetime
    evidence_revision: str
    windows: list[TimelineCoverageWindow]
    evidence: list[TimelineEvidenceItem]


class AgentAssertion(BaseModel):
    claim: str = Field(min_length=1, max_length=500)
    role: EvidenceRole
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class AgentAttribute(BaseModel):
    """A strict-schema-safe episode attribute.

    Codex structured outputs cannot represent an object with arbitrary keys, so the
    agent emits key/value entries and Chronicle materializes them as an object when
    publishing the episode.
    """

    key: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=500)


class AgentEpisode(BaseModel):
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=1200)
    started_at: datetime
    ended_at: datetime
    # Factual, not a memory-worthiness judgement: did people actually converse here.
    # It is what promotes capture-evidence recordings back into the user-facing
    # Recordings list and search. What is worth *remembering* stays the vault write
    # agent's decision — see services/timeline/memory.py.
    conversational: bool = False
    salience: Literal["background", "routine", "notable", "highlight"]
    activity_mode: Literal["foreground", "background", "ambient", "idle"]
    confidence: float = Field(ge=0, le=1)
    entities: list[str] = Field(default_factory=list)
    attributes: list[AgentAttribute] = Field(default_factory=list)
    assertions: list[AgentAssertion] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    related_conversation_ids: list[str] = Field(default_factory=list)
    parent_episode_index: Optional[int] = Field(default=None, ge=0)
    representative_evidence_id: Optional[str] = None

    @model_validator(mode="after")
    def positive_duration(self) -> "AgentEpisode":
        """Give an instantaneous episode the smallest real duration instead of rejecting it.

        Some things the agent identifies genuinely happen at a point in time — a photo
        review, a button press, a `user_action` — and it reports them with
        ``ended_at == started_at``. Raising here failed the *whole* ``TimelineAgentResult``,
        so one instant out of twelve episodes cost an entire day its memory after a full
        agent run, and the failure was deterministic: a retry re-derived the same episode
        and failed identically. Measured on 2026-07-28 and 2026-07-29 during the
        full-corpus rebuild.

        An inverted range is still a real error — that is the agent contradicting itself,
        not describing an instant — so it is left to raise.
        """
        if self.ended_at == self.started_at:
            self.ended_at = self.started_at + timedelta(seconds=1)
        elif self.ended_at < self.started_at:
            raise ValueError("episode ends before it starts")
        return self


class UnassignedInterval(BaseModel):
    started_at: datetime
    ended_at: datetime
    reason: str
    # Derived from the manifest after parsing, never from the agent. "Nothing was
    # captured" and "capture exists but no episode explains it" are different facts,
    # and agent-authored `reason` prose conflates them freely: one observed run
    # labelled a three-hour recording blackout "no evidence supports a coherent
    # episode". Only the second cause is a segmentation deficiency.
    cause: Literal["no_capture", "unexplained"] = "unexplained"


class TimelineAgentResult(BaseModel):
    episodes: list[AgentEpisode]
    unassigned_intervals: list[UnassignedInterval] = Field(default_factory=list)
    # Filled by the executor after parsing, not by the model: token counts and quota
    # headroom for the run. Not part of OUTPUT_SCHEMA.
    usage: dict[str, Any] = Field(default_factory=dict)


class EvidenceBundle(BaseModel):
    """Everything a reconciliation run may consider for one absolute range.

    Produced by ``load_reconciliation_evidence`` (the range core extracted from
    ``assemble_day_evidence``). ``local_date``/``timezone`` on the manifest are
    derived from the range start in the user's timezone — projection hints, not
    authority. ``existing_episodes`` and ``pinned_episodes`` carry the prior
    interpretation so a run revises rather than rederives.
    """

    manifest: TimelineEvidenceManifest
    # Serialized active TimelineEpisode revisions intersecting the range
    # (rolling pipeline rows only).
    existing_episodes: list[dict[str, Any]] = Field(default_factory=list)
    # Human-pinned episodes/boundaries the agent must not cross.
    pinned_episodes: list[dict[str, Any]] = Field(default_factory=list)
    # The per-user evidence-revision counter value this bundle reflects.
    evidence_revision: int = 0


class RequestMoreContext(BaseModel):
    action: Literal["request_more_context"] = "request_more_context"
    left_seconds: float = Field(default=0, ge=0)
    right_seconds: float = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=500)


class WaitForFutureEvidence(BaseModel):
    action: Literal["wait_for_future_evidence"] = "wait_for_future_evidence"
    reason: str = Field(min_length=1, max_length=500)


class Publish(BaseModel):
    action: Literal["publish"] = "publish"
    result: TimelineAgentResult


# One reconciliation-loop step: publish revisions, ask for bounded expansion on a
# side, or park until future evidence arrives. Chronicle enforces the budgets
# (5-minute increments per side per iteration, ≤6 iterations ⇒ ≤30 min/side).
ReconcileAction = Publish | RequestMoreContext | WaitForFutureEvidence


class PublishResult(BaseModel):
    """Outcome of atomically publishing one reconciliation generation."""

    episode_ids: list[str] = Field(default_factory=list)
    episode_keys: list[str] = Field(default_factory=list)
    superseded_episode_ids: list[str] = Field(default_factory=list)
    # Local dates whose day projections the publish touched (both, for a
    # cross-midnight episode).
    affected_local_dates: list[date] = Field(default_factory=list)
    # False when the CAS fence on the leased evidence revision failed; the caller
    # marks the range stale and the re-dirtied range retries.
    fenced: bool = True
    material_change: bool = False


class TimelineEpisodeExecutor(Protocol):
    async def analyze(
        self,
        workspace: Path,
        manifest: TimelineEvidenceManifest,
        existing_episodes: list[dict[str, Any]],
        pinned_episodes: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
        validation_feedback: str | None = None,
    ) -> TimelineAgentResult: ...
