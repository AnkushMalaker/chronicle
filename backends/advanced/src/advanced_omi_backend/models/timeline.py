"""Durable evidence and revisioned semantic timeline models."""

import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


EvidenceKind = Literal[
    "audio_span",
    "transcript",
    "observation",
    "frame",
    "meeting",
    "immich",
    "capture_gap",
]
EvidenceRole = Literal[
    "user_action",
    "user_statement",
    "third_party",
    "application_state",
    "media_content",
    "assistant_generated",
    "ambient",
    "uncertain",
]


class TimelineEvidenceRef(BaseModel):
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
    # Assembly-time metadata (``conversation_id``, image content type, app/window
    # identity). Persisted so an episode can link back to the artifact it cites.
    metadata: dict[str, Any] = Field(default_factory=dict)


class TimelineAssertion(BaseModel):
    claim: str = Field(min_length=1, max_length=500)
    role: EvidenceRole
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class AudioEvidenceSpan(Document):
    """Compact profile for one assembled ScreenPipe compute span.

    Thirty-second source files remain transport units. This document is the durable
    compute unit and stores parallel 10-second series so downstream windows can slice
    the profile without duplicating per-bucket documents.
    """

    user_id: str
    source_id: str
    source_item_ids: list[str]
    first_source_item_id: str
    last_source_item_id: str
    source_range_hash: str
    started_at: datetime
    ended_at: datetime
    direction: Literal["input", "output", "unknown"] = "unknown"
    meeting_id: Optional[str] = None
    conversation_id: Optional[str] = None
    state: Literal["transcribed", "no_speech", "unscored", "failed"]
    covered_seconds: float = Field(ge=0)
    missing_seconds: float = Field(ge=0)
    bucket_seconds: float = Field(default=10.0, gt=0)
    coverage_fraction: list[float] = Field(default_factory=list)
    speech_fraction: list[Optional[float]] = Field(default_factory=list)
    acoustic_active_fraction: list[float] = Field(default_factory=list)
    rms_dbfs: list[Optional[float]] = Field(default_factory=list)
    peak_dbfs: list[Optional[float]] = Field(default_factory=list)
    speech_seconds: Optional[float] = Field(default=None, ge=0)
    longest_no_speech_seconds: Optional[float] = Field(default=None, ge=0)
    acoustic_active_seconds: float = Field(default=0, ge=0)
    acoustic_quiet_seconds: float = Field(default=0, ge=0)
    word_count: Optional[int] = Field(default=None, ge=0)
    analysis: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_series(self) -> "AudioEvidenceSpan":
        if self.ended_at <= self.started_at:
            raise ValueError("audio evidence span must have positive duration")
        lengths = {
            len(self.coverage_fraction),
            len(self.speech_fraction),
            len(self.acoustic_active_fraction),
            len(self.rms_dbfs),
            len(self.peak_dbfs),
        }
        if len(lengths) != 1:
            raise ValueError("audio evidence bucket series must have equal lengths")
        return self

    class Settings:
        name = "audio_evidence_spans"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("source_id", ASCENDING),
                    ("direction", ASCENDING),
                    ("first_source_item_id", ASCENDING),
                    ("last_source_item_id", ASCENDING),
                ],
                unique=True,
                name="audio_evidence_source_range",
            ),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("started_at", ASCENDING),
                    ("ended_at", ASCENDING),
                ]
            ),
        ]


class TimelineAnalysisRun(Document):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    local_date: date
    timezone: str
    day_started_at: datetime
    day_ended_at: datetime
    evidence_revision: str
    processed_evidence_revision: Optional[str] = None
    prompt_version: str
    executor: str
    state: Literal[
        "pending",
        "preparing",
        "awaiting_evidence",
        "running",
        "validating",
        "complete",
        "failed",
        "quota_deferred",
    ] = "pending"
    claimed_at: Optional[datetime] = None
    retry_after: Optional[datetime] = None
    attempts: int = 0
    coverage_window_ids: list[str] = Field(default_factory=list)
    output_episode_ids: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    usage: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: Optional[datetime] = None

    class Settings:
        name = "timeline_analysis_runs"
        indexes = [
            IndexModel([("run_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("local_date", ASCENDING),
                    ("timezone", ASCENDING),
                    ("evidence_revision", ASCENDING),
                    ("prompt_version", ASCENDING),
                ],
                unique=True,
                name="timeline_analysis_revision",
            ),
            IndexModel([("state", ASCENDING), ("created_at", ASCENDING)]),
        ]


class TimelineEpisode(Document):
    episode_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Durable identity across analysis runs. ``episode_id`` names this row; a confirmed
    # episode keeps its ``episode_key`` when it is carried into the next generation.
    episode_key: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    user_id: str
    local_date: date
    timezone: str
    started_at: datetime
    ended_at: datetime
    kind: str
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=1200)
    # Agent output is provisional until a person edits it. A "confirmed" default would
    # pin every generated episode and make reanalysis a no-op.
    status: Literal["provisional", "confirmed", "superseded"] = "provisional"
    salience: Literal["background", "routine", "notable", "highlight"] = "routine"
    confidence: float = Field(ge=0, le=1)
    activity_mode: Literal["foreground", "background", "ambient", "idle"]
    entities: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    assertions: list[TimelineAssertion] = Field(default_factory=list)
    evidence_refs: list[TimelineEvidenceRef] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    related_episode_ids: list[str] = Field(default_factory=list)
    related_conversation_ids: list[str] = Field(default_factory=list)
    parent_episode_id: Optional[str] = None
    representative_image: Optional[bytes] = None
    representative_image_type: Optional[str] = None
    # Frames sampled across this episode's own interval, fetched from the node's full
    # ScreenPipe store rather than inherited from whatever an observation happened to
    # shortlist. Transient: the picker keeps one as `representative_image` and clears
    # the rest. `thumbnail_state` drives the pass — "" not yet requested, "requested"
    # awaiting the node, "chosen"/"unavailable" terminal.
    frame_shortlist: list[dict[str, Any]] = Field(default_factory=list)
    thumbnail_state: Literal["", "requested", "chosen", "unavailable"] = ""
    # Human confirmation. A confirmed episode is an anchor: later runs carry it forward
    # verbatim instead of regenerating its interval. ``confirmed_fields`` records which
    # fields the person owns, so agent-derived fields stay free to refresh.
    confirmed_at: Optional[datetime] = None
    confirmed_fields: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    revised_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_episode(self) -> "TimelineEpisode":
        if self.ended_at <= self.started_at:
            raise ValueError("timeline episode must have positive duration")
        known_ids = {ref.evidence_id for ref in self.evidence_refs}
        for assertion in self.assertions:
            if not set(assertion.evidence_ids).issubset(known_ids):
                raise ValueError("timeline assertion references unknown evidence")
        return self

    class Settings:
        name = "timeline_episodes"
        indexes = [
            IndexModel([("episode_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("local_date", DESCENDING)]),
            IndexModel([("run_id", ASCENDING), ("started_at", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("episode_key", ASCENDING)]),
        ]


class TimelineDay(Document):
    user_id: str
    local_date: date
    timezone: str
    active_run_id: Optional[str] = None
    active_run_created_at: Optional[datetime] = None
    evidence_revision: Optional[str] = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    revised_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "timeline_days"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("local_date", ASCENDING),
                    ("timezone", ASCENDING),
                ],
                unique=True,
                name="timeline_day_identity",
            )
        ]
