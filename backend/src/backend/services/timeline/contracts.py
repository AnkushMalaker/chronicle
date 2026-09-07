"""Executor-independent contracts for semantic timeline analysis."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.timeline import (
    EvidenceAnchor,
    EvidenceKind,
    EvidenceLocator,
    EvidenceRole,
)


class EvidenceCoverage(BaseModel):
    """Auditable thinning counts for one source evidence record."""

    source_count: int = Field(default=0, ge=0)
    retained_count: int = Field(default=0, ge=0)
    agent_visible_count: int = Field(default=0, ge=0)
    cited_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "EvidenceCoverage":
        if self.retained_count > self.source_count:
            raise ValueError("retained evidence count exceeds source count")
        if self.agent_visible_count > self.retained_count:
            raise ValueError("agent-visible evidence count exceeds retained count")
        if self.cited_count > self.agent_visible_count:
            raise ValueError("cited evidence count exceeds agent-visible count")
        return self


class TimelineEvidenceItem(BaseModel):
    evidence_id: str
    kind: EvidenceKind
    source_id: Optional[str] = None
    source_item_id: Optional[str] = None
    locator: EvidenceLocator
    started_at: datetime
    ended_at: Optional[datetime] = None
    role: EvidenceRole
    excerpt: Optional[str] = None
    content_hash: Optional[str] = None
    ephemeral: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    image_filename: Optional[str] = None
    anchor_ids: list[str] = Field(default_factory=list)
    coverage: EvidenceCoverage = Field(default_factory=EvidenceCoverage)


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
    anchors: list[EvidenceAnchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_manifest_references(self) -> "TimelineEvidenceManifest":
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        if len(evidence_by_id) != len(self.evidence):
            raise ValueError("timeline evidence IDs must be unique")
        anchors_by_id = {anchor.anchor_id: anchor for anchor in self.anchors}
        if len(anchors_by_id) != len(self.anchors):
            raise ValueError("timeline evidence anchor IDs must be unique")
        for anchor in self.anchors:
            item = evidence_by_id.get(anchor.evidence_id)
            if item is None:
                raise ValueError(
                    f"anchor {anchor.anchor_id!r} references unknown evidence "
                    f"{anchor.evidence_id!r}"
                )
            if item.locator != anchor.locator:
                raise ValueError(
                    f"anchor {anchor.anchor_id!r} locator does not match its evidence"
                )
            if anchor.anchor_id not in item.anchor_ids:
                raise ValueError(
                    f"evidence {item.evidence_id!r} does not declare anchor "
                    f"{anchor.anchor_id!r}"
                )
        for item in self.evidence:
            unknown = sorted(set(item.anchor_ids) - anchors_by_id.keys())
            if unknown:
                raise ValueError(
                    f"evidence {item.evidence_id!r} references unknown anchors {unknown}"
                )
        return self

    def resolve_anchor(
        self, anchor_id: str, *, evidence_id: str | None = None
    ) -> EvidenceAnchor:
        """Resolve a boundary citation against the complete authoritative manifest."""

        anchor = next(
            (item for item in self.anchors if item.anchor_id == anchor_id), None
        )
        if anchor is None:
            raise ValueError(f"unknown evidence anchor: {anchor_id}")
        if evidence_id is not None and anchor.evidence_id != evidence_id:
            raise ValueError(
                f"anchor {anchor_id!r} does not support evidence {evidence_id!r}"
            )
        return anchor

    def resolve_boundary_anchor(
        self, *, evidence_id: str, anchor_id: str, boundary_at: datetime
    ) -> EvidenceAnchor:
        """Reject an arbitrary interior instant not supported by a cited anchor."""

        anchor = self.resolve_anchor(anchor_id, evidence_id=evidence_id)
        moment = (
            boundary_at
            if boundary_at.tzinfo
            else boundary_at.replace(tzinfo=anchor.earliest_at.tzinfo)
        )
        earliest = anchor.earliest_at
        latest = anchor.latest_at
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=moment.tzinfo)
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=moment.tzinfo)
        if not earliest <= moment <= latest:
            raise ValueError(
                f"boundary {boundary_at.isoformat()} is outside anchor "
                f"{anchor_id!r} support window"
            )
        return anchor


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
        ``ended_at == started_at``. Raising here failed the whole validated projection,
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


class ValidatedTimelineProjection(BaseModel):
    """Post-barrier episode projection assembled from both validated stages.

    This is not model output: structure comes only from ``SeparationResult`` and
    semantics come only from ``InterpretationResult``.
    """

    episodes: list[AgentEpisode]
    unassigned_intervals: list[UnassignedInterval] = Field(default_factory=list)


class _StrictStageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageInferenceProvenance(BaseModel):
    """Content-addressed model interaction that produced one validated stage."""

    operation: str = Field(min_length=1)
    request_hash: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)
    cache_hit: bool = False


class EpisodeRevisionRef(_StrictStageModel):
    episode_key: str = Field(min_length=1)
    revision: int = Field(ge=1)


class EpisodeLineageProposal(_StrictStageModel):
    action: Literal["new", "carry", "split", "merge"]
    predecessor_revisions: list[EpisodeRevisionRef] = Field(default_factory=list)


class EpisodeRetirementProposal(_StrictStageModel):
    predecessor_revision: EpisodeRevisionRef
    reason: str = Field(min_length=1, max_length=500)


class UnresolvedInterval(UnassignedInterval):
    """Time the separator cannot truthfully assign from the current evidence."""

    model_config = ConfigDict(extra="forbid")


class SeparatedEpisode(_StrictStageModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    started_at: datetime
    ended_at: datetime
    evidence_ids: list[str] = Field(min_length=1)
    start_anchor_ids: list[str] = Field(min_length=1)
    end_anchor_ids: list[str] = Field(min_length=1)
    lineage: EpisodeLineageProposal
    confidence: float = Field(ge=0, le=1)
    review_reasons: list[str] = Field(default_factory=list)


class SeparationResult(_StrictStageModel):
    hypotheses: list[SeparatedEpisode] = Field(default_factory=list)
    retirements: list[EpisodeRetirementProposal] = Field(default_factory=list)
    unassigned_evidence_ids: list[str] = Field(default_factory=list)
    unresolved_intervals: list[UnresolvedInterval] = Field(default_factory=list)
    context_requests: list["StageContextRequest"] = Field(
        default_factory=list, max_length=1
    )
    # Executor-owned, never part of the model output contract.
    inference_provenance: StageInferenceProvenance | None = Field(
        default=None, exclude=True
    )


class InterpretedEpisode(_StrictStageModel):
    """Semantic fields joined to one immutable structural hypothesis."""

    hypothesis_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=1200)
    conversational: bool = False
    salience: Literal["background", "routine", "notable", "highlight"]
    activity_mode: Literal["foreground", "background", "ambient", "idle"]
    confidence: float = Field(ge=0, le=1)
    entities: list[str] = Field(default_factory=list)
    attributes: list[AgentAttribute] = Field(default_factory=list)
    assertions: list[AgentAssertion] = Field(default_factory=list)
    related_conversation_ids: list[str] = Field(default_factory=list)
    parent_episode_index: int | None = Field(default=None, ge=0)
    representative_evidence_id: str | None = None


class RejectedHypothesis(_StrictStageModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    reason_code: Literal[
        "incoherent", "mixed_activities", "insufficient_context", "redundant_activity"
    ]
    explanation: str = Field(min_length=1, max_length=1000)
    implicated_evidence_ids: list[str] = Field(default_factory=list)


class InterpretationResult(_StrictStageModel):
    accepted: list[InterpretedEpisode] = Field(default_factory=list)
    rejected: list[RejectedHypothesis] = Field(default_factory=list)
    context_requests: list["StageContextRequest"] = Field(
        default_factory=list, max_length=1
    )
    # Executor-owned, never part of the model output contract.
    inference_provenance: StageInferenceProvenance | None = Field(
        default=None, exclude=True
    )


class StageContextRequest(_StrictStageModel):
    context_request_id: str = Field(min_length=1)
    hypothesis_id: str | None = None
    stage: Literal["separation", "interpretation"]
    locator: EvidenceLocator
    started_at: datetime
    ended_at: datetime
    base_manifest_hash: str = Field(min_length=1)
    leased_evidence_revision: int = Field(ge=0)
    target_resolution: str = Field(min_length=1)
    max_items: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


SeparationResult.model_rebuild()
InterpretationResult.model_rebuild()


class EvidenceBundle(BaseModel):
    """Everything a reconciliation run may consider for one absolute range.

    Produced by ``load_reconciliation_evidence`` (the range core extracted from
    ``assemble_day_evidence``). ``local_date``/``timezone`` on the manifest are
    derived from the range start in the user's timezone — projection hints, not
    authority. ``existing_episodes`` and ``pinned_episodes`` carry the prior
    interpretation so a run revises rather than rederives.
    """

    manifest: TimelineEvidenceManifest
    activity_rejections: list[dict[str, Any]] = Field(default_factory=list)
    # Serialized active TimelineEpisode revisions intersecting the range
    # from the rolling episode ledger.
    existing_episodes: list[dict[str, Any]] = Field(default_factory=list)
    # Human-pinned episodes/boundaries the agent must not cross.
    pinned_episodes: list[dict[str, Any]] = Field(default_factory=list)
    # The per-user evidence-revision counter value this bundle reflects.
    evidence_revision: int = 0


class RequestMoreContext(BaseModel):
    action: Literal["request_more_context"] = "request_more_context"
    request: StageContextRequest


class WaitForFutureEvidence(BaseModel):
    action: Literal["wait_for_future_evidence"] = "wait_for_future_evidence"
    reason: str = Field(min_length=1, max_length=500)


class Publish(BaseModel):
    action: Literal["publish"] = "publish"
    projection: ValidatedTimelineProjection
    separation: SeparationResult
    interpretation: InterpretationResult
    separation_inference: StageInferenceProvenance
    interpretation_inference: StageInferenceProvenance


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
    async def separate(
        self,
        workspace: Path,
        bundle: EvidenceBundle,
        *,
        reasoning_effort: str | None = None,
        validation_feedback: str | None = None,
        validate_result: Callable[[SeparationResult], None] | None = None,
    ) -> SeparationResult: ...

    async def interpret(
        self,
        workspace: Path,
        bundle: EvidenceBundle,
        separation: SeparationResult,
        *,
        reasoning_effort: str | None = None,
        validate_result: Callable[[InterpretationResult], None] | None = None,
    ) -> InterpretationResult: ...
