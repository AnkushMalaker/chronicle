"""Executor-independent contracts for semantic timeline analysis."""

from datetime import date, datetime
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


class AgentEpisode(BaseModel):
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=1200)
    started_at: datetime
    ended_at: datetime
    salience: Literal["background", "routine", "notable", "highlight"]
    activity_mode: Literal["foreground", "background", "ambient", "idle"]
    confidence: float = Field(ge=0, le=1)
    entities: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    assertions: list[AgentAssertion] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    related_conversation_ids: list[str] = Field(default_factory=list)
    parent_episode_index: Optional[int] = Field(default=None, ge=0)
    representative_evidence_id: Optional[str] = None

    @model_validator(mode="after")
    def positive_duration(self) -> "AgentEpisode":
        if self.ended_at <= self.started_at:
            raise ValueError("episode must have positive duration")
        return self


class UnassignedInterval(BaseModel):
    started_at: datetime
    ended_at: datetime
    reason: str


class TimelineAgentResult(BaseModel):
    covered_window_ids: list[str]
    episodes: list[AgentEpisode]
    unassigned_intervals: list[UnassignedInterval] = Field(default_factory=list)


class TimelineEpisodeExecutor(Protocol):
    async def analyze(
        self,
        workspace: Path,
        manifest: TimelineEvidenceManifest,
        existing_episodes: list[dict[str, Any]],
    ) -> TimelineAgentResult: ...
