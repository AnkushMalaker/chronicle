"""Durable evidence and revisioned semantic timeline models."""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
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


class TimelineAudioRange(BaseModel):
    """Immutable audio-document references supporting one episode interval.

    Chunk ids and absolute bounds remain valid when an operational conversation is
    split, merged, or silence-trimmed. ``conversation_ids`` is lineage/debug context;
    consumers must not use it as the range's identity.
    """

    range_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chunk_ids: list[str] = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    source_stream: Optional[str] = None
    conversation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_range(self) -> "TimelineAudioRange":
        if self.ended_at <= self.started_at:
            raise ValueError("timeline audio range must have positive duration")
        return self


def _utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; compare everything in UTC."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def clip_audio_ranges(
    ranges: Sequence[TimelineAudioRange],
    start: datetime,
    end: datetime,
    chunk_spans: Mapping[str, Optional[tuple[datetime, float]]],
    *,
    keep_unplaceable: bool,
) -> list[TimelineAudioRange]:
    """The audio claim of ``ranges`` restricted to ``[start, end)``.

    An episode's ranges are its claim over real audio, so cutting the episode has to
    cut the claim too. Splitting used to deep-copy every range onto both halves, which
    made each half claim the whole original recording — both sides played the same
    audio, and each reported the other's recordings as its own.

    ``chunk_spans`` maps a chunk id to its ``(captured_at, duration)``; the caller
    supplies it so this stays a pure function. A chunk whose span is unknown — 3% of
    this deployment's chunks predate ``captured_at`` — cannot be placed on either side
    of the cut, so ``keep_unplaceable`` decides where it goes. Callers give it to the
    surviving head only: dropping it would lose a reference that a later
    ``captured_at`` backfill could still resolve, and putting it on both sides would
    reinstate exactly the double-claim being fixed.
    """

    start, end = _utc(start), _utc(end)
    clipped: list[TimelineAudioRange] = []

    for audio_range in ranges:
        range_start = max(_utc(audio_range.started_at), start)
        range_end = min(_utc(audio_range.ended_at), end)
        if range_end <= range_start:
            continue

        chunk_ids = []
        for chunk_id in audio_range.chunk_ids:
            span = chunk_spans.get(chunk_id)
            if span is None:
                if keep_unplaceable:
                    chunk_ids.append(chunk_id)
                continue
            captured_at, duration = span
            captured_at = _utc(captured_at)
            if (
                captured_at < range_end
                and captured_at + timedelta(seconds=duration) > range_start
            ):
                chunk_ids.append(chunk_id)

        # ``chunk_ids`` is min_length=1: a window covering none of this range's audio
        # is not a claim at all, so the range is dropped rather than emptied.
        if not chunk_ids:
            continue

        clipped.append(
            audio_range.model_copy(
                update={
                    "chunk_ids": chunk_ids,
                    "started_at": range_start,
                    "ended_at": range_end,
                }
            )
        )

    return clipped


def merge_audio_ranges(
    groups: Iterable[Sequence[TimelineAudioRange]],
) -> list[TimelineAudioRange]:
    """The union of several episodes' audio claims, in wall-clock order.

    Merging used to union evidence, entities and conversation ids while leaving
    ``audio_ranges`` untouched, so the survivor kept only its own audio and every
    absorbed episode's went to the grave with its document — a merged episode covering
    three recordings could play one.

    Ranges are identified by ``range_id`` and are immutable, so a union deduplicated on
    that id needs no interval arithmetic: two episodes citing the same range cite the
    same audio.
    """

    merged: dict[str, TimelineAudioRange] = {}
    for group in groups:
        for audio_range in group:
            merged.setdefault(audio_range.range_id, audio_range)
    return sorted(merged.values(), key=lambda item: _utc(item.started_at))


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
    state: Literal["transcribed", "no_speech", "unscored", "failed", "abandoned"]
    # Ingest attempts for this range. A failed upload leaves the staged chunks in
    # place so the next tick can retry; without a bound that retry is forever, and
    # each pass leaks another Conversation holding a full copy of the audio.
    attempts: int = Field(default=0, ge=0)
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
    # People actually talked with each other here. Promotes the cited capture-evidence
    # recordings back into the user-facing Recordings list and search — see
    # services/timeline/discovery.py. Factual; ``salience`` carries importance.
    conversational: bool = False
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
    # Authoritative playable audio. Unlike related_conversation_ids, these references
    # survive container replacement because Mongo chunk ids and captured_at are stable.
    audio_ranges: list[TimelineAudioRange] = Field(default_factory=list)
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
    # Per-episode record of the settled-day vault write, for resumability within a day
    # and for showing provenance. The authoritative latch is ``TimelineDay.memory_state``
    # — episode rows are per-run and do not survive regeneration.
    memory_state: Literal["", "written", "partial", "skipped"] = ""
    vault_paths: list[str] = Field(default_factory=list)
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
    # Authoritative latch for the settled-day vault write. It is valid only while
    # ``memory_run_id == active_run_id``. Publishing a newer generation clears the
    # latch so the canonical Daily episode index cannot silently retain stale bounds.
    # ``claimed`` is held only while the agent runs.
    # ``no_changes`` is terminal like ``written``: the agent read the day and judged it
    # already recorded. Distinct from ``written`` so a day the vault never gained
    # anything from is visible, and from ``skipped`` which means there was no analysis
    # to record at all.
    # ``partial`` is terminal too: the agent was truncated or stalled after producing a
    # structurally valid day note, so audited mutations are kept but the run may never
    # have reached its People/Topic edits. It is not retried, because truncation
    # belongs to the model's round limit rather than to this day — republishing the
    # day's analysis clears the latch and writes it properly.
    memory_state: Literal[
        "", "claimed", "written", "partial", "skipped", "no_changes"
    ] = ""
    memory_run_id: Optional[str] = None
    memory_claimed_at: Optional[datetime] = None
    memory_written_at: Optional[datetime] = None
    # Bounded retry. A day that keeps failing settles into ``skipped`` with its last
    # diagnostic rather than being re-attempted on every tick forever.
    memory_attempts: int = 0
    memory_error: Optional[str] = None
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
            ),
            IndexModel(
                [("memory_state", ASCENDING), ("local_date", ASCENDING)],
                name="timeline_day_memory_state",
            ),
        ]
