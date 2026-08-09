"""Bulk reconstruction of derived Markdown memories from durable transcripts."""

from __future__ import annotations

import os
import tarfile
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    default_queue,
    memory_queue,
    post_conv_enqueue_kwargs,
    transcription_queue,
)
from advanced_omi_backend.services.data_archive import clear_vault_contents
from advanced_omi_backend.services.memory.audit import MemoryCause, UpdateStrategy
from advanced_omi_backend.services.timeline.timezone import canonical_timezone
from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

VAULT_ROOTS = ("conversation_docs", "memory_md")
MEMORY_REBUILD_JOB_TIMEOUT = 7200
# One day = a segmentation agent run over the day's evidence plus a vault write agent
# run; both are slow, and a dense day escalates the segmentation effort.
TIMELINE_REBUILD_JOB_TIMEOUT = 10800

import logging

logger = logging.getLogger(__name__)


class MemoryRebuildError(RuntimeError):
    """Raised when a clean memory rebuild cannot start safely."""


class RebuildStage(str, Enum):
    """Earliest derived-data stage to rerun during reconstruction.

    ``TIMELINE`` is the earliest because episode bounds decide what a memory is *about*.
    Replaying memory alone reproduces the old container boundaries — the very thing a
    re-bound exists to replace — so a rebuild that changed audio bounds must start here.

    ``DAYS`` is ``TIMELINE`` without the diarization: it re-decides every boundary and
    rewrites the vault over the speaker layer that is already there. Use it when what
    changed is the segmentation agent, the day prompt, or the episode-note format —
    none of which alter a transcript, so paying for a full speaker fan-out (hundreds of
    GPU jobs, hours) to reach a day chain that runs in minutes is pure waste.
    """

    MEMORY = "memory"
    SPEAKERS = "speakers"
    DAYS = "days"
    TIMELINE = "timeline"


# Stages that re-run diarization, and so must read the ASR layer rather than whatever
# speaker layer is sitting on top of it. DAYS is deliberately absent: reading the
# speaker layer is the whole point of it.
_DIARIZING_STAGES = frozenset({RebuildStage.SPEAKERS, RebuildStage.TIMELINE})

# Stages that re-decide episode boundaries and rewrite the vault from the day pass.
TIMELINE_STAGES = frozenset({RebuildStage.DAYS, RebuildStage.TIMELINE})


@dataclass(frozen=True)
class RebuildConversation:
    conversation_id: str
    user_id: str
    created_at: Any
    transcript_version_id: str
    memory_excluded: bool = False
    has_audio: bool = True
    active_transcript_version_id: Optional[str] = None


@dataclass(frozen=True)
class RebuildPlan:
    conversations: tuple[RebuildConversation, ...]
    user_ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.conversations)

    @property
    def memory_count(self) -> int:
        return sum(not item.memory_excluded for item in self.conversations)

    @property
    def speaker_count(self) -> int:
        return sum(item.has_audio for item in self.conversations)


@dataclass(frozen=True)
class RebuildDay:
    user_id: str
    local_date: date
    timezone: str


@dataclass(frozen=True)
class RebuildResult:
    run_id: str
    jobs: tuple[str, ...]
    speaker_jobs: tuple[str, ...]
    skipped_speaker_conversations: tuple[str, ...]
    memory_jobs: tuple[str, ...]
    from_stage: RebuildStage
    user_ids: tuple[str, ...]
    deleted_vault_files: int
    deleted_audit_entries: int
    vault_backup: Optional[Path]
    timeline_jobs: tuple[str, ...] = ()
    timeline_days: tuple[RebuildDay, ...] = ()
    deleted_timeline_documents: int = 0


def _normalise_user_ids(user_ids: Optional[Iterable[str]]) -> tuple[str, ...]:
    if not user_ids:
        return ()
    normalised = tuple(sorted({str(user_id) for user_id in user_ids}))
    if any(not user_id or Path(user_id).name != user_id for user_id in normalised):
        raise MemoryRebuildError("Invalid user ID")
    return normalised


def _speaker_source_version(document: dict[str, Any]) -> str:
    """Walk back prior speaker-only versions to the underlying ASR transcript."""
    active_id = document["active_transcript_version"]
    versions = {
        version.get("version_id"): version
        for version in document.get("transcript_versions", [])
        if isinstance(version, dict) and version.get("version_id")
    }
    current_id = active_id
    visited: set[str] = set()
    while current_id not in visited:
        visited.add(current_id)
        version = versions.get(current_id) or {}
        metadata = version.get("metadata") or {}
        if metadata.get("reprocessing_type") != "speaker_diarization":
            break
        source_id = metadata.get("source_version_id")
        if not source_id or source_id not in versions:
            break
        current_id = source_id
    return current_id


async def build_rebuild_plan(
    database: Any,
    user_ids: Optional[Iterable[str]] = None,
    *,
    from_stage: RebuildStage = RebuildStage.MEMORY,
) -> RebuildPlan:
    """Select replayable conversations in stable chronological order."""
    requested_users = _normalise_user_ids(user_ids)
    query: dict[str, Any] = {
        "active_transcript_version": {"$ne": None},
        "deleted": {"$ne": True},
    }
    if from_stage is RebuildStage.MEMORY:
        query["memory_excluded"] = {"$ne": True}
    if requested_users:
        query["user_id"] = {"$in": list(requested_users)}

    cursor = (
        database["conversations"]
        .find(
            query,
            projection={
                "conversation_id": 1,
                "user_id": 1,
                "created_at": 1,
                "active_transcript_version": 1,
                "transcript_versions.version_id": 1,
                "transcript_versions.metadata": 1,
                "memory_excluded": 1,
            },
        )
        .sort([("user_id", 1), ("created_at", 1), ("conversation_id", 1)])
    )
    conversations_list: list[RebuildConversation] = []
    async for document in cursor:
        conversations_list.append(
            RebuildConversation(
                conversation_id=document["conversation_id"],
                user_id=str(document["user_id"]),
                created_at=document.get("created_at"),
                transcript_version_id=(
                    _speaker_source_version(document)
                    if from_stage in _DIARIZING_STAGES
                    else document["active_transcript_version"]
                ),
                memory_excluded=document.get("memory_excluded", False) is True,
                active_transcript_version_id=document["active_transcript_version"],
            )
        )
    if from_stage in _DIARIZING_STAGES and conversations_list:
        conversation_ids = [item.conversation_id for item in conversations_list]
        audio_conversation_ids = set(
            await database["audio_chunks"].distinct(
                "conversation_id",
                {
                    "conversation_id": {"$in": conversation_ids},
                    "deleted": {"$ne": True},
                },
            )
        )
        conversations_list = [
            replace(item, has_audio=item.conversation_id in audio_conversation_ids)
            for item in conversations_list
        ]
    conversations = tuple(conversations_list)
    selected_users = tuple(sorted({item.user_id for item in conversations}))
    if requested_users:
        selected_users = requested_users
    return RebuildPlan(conversations=conversations, user_ids=selected_users)


async def build_timeline_days(
    database: Any, user_ids: tuple[str, ...]
) -> tuple[RebuildDay, ...]:
    """Every local day these users captured audio on, oldest first.

    Days are derived from each chunk's ``captured_at`` rather than the owning
    conversation's ``created_at``: a re-bound or uploaded recording is filed on the day
    it was *recorded*, and those differ by up to several days here. A recording that
    straddles local midnight belongs to both days, so the span is expanded, not rounded.
    """

    if not user_ids:
        return ()
    # Raw collection rather than the Beanie model: this module is driven by the
    # chronicle_data CLI, which connects without initializing document models.
    zones: dict[str, str] = {}
    async for user in database["users"].find(
        {"_id": {"$in": [PydanticObjectId(item) for item in user_ids]}},
        projection={"timezone": 1},
    ):
        zones[str(user["_id"])] = canonical_timezone(user.get("timezone") or "UTC")

    owners: dict[str, str] = {}
    cursor = database["conversations"].find(
        {
            "deleted": {"$ne": True},
            "data_purpose": {"$ne": "annotation"},
            "user_id": {"$in": list(user_ids)},
        },
        projection={"conversation_id": 1, "user_id": 1},
    )
    async for document in cursor:
        owners[document["conversation_id"]] = str(document["user_id"])
    if not owners:
        return ()

    rows = (
        await database["audio_chunks"]
        .aggregate(
            [
                {
                    "$match": {
                        "conversation_id": {"$in": list(owners)},
                        "deleted": {"$ne": True},
                        "captured_at": {"$ne": None},
                    }
                },
                {
                    "$group": {
                        "_id": "$conversation_id",
                        "first": {"$min": "$captured_at"},
                        "last": {"$max": "$captured_at"},
                    }
                },
            ],
            allowDiskUse=True,
        )
        .to_list(length=None)
    )

    days: set[tuple[str, date, str]] = set()
    for row in rows:
        user_id = owners.get(row["_id"])
        if not user_id:
            continue
        zone_name = zones.get(user_id, "UTC")
        zone = ZoneInfo(zone_name)
        first = _local_date(row["first"], zone)
        last = _local_date(row["last"], zone)
        current = first
        while current <= last:
            days.add((user_id, current, zone_name))
            current += timedelta(days=1)
    return tuple(
        RebuildDay(user_id=user_id, local_date=local_date, timezone=zone_name)
        for user_id, local_date, zone_name in sorted(days)
    )


def _local_date(value: datetime, zone: ZoneInfo) -> date:
    """Which local day this capture instant falls on.

    Mongo returns ``captured_at`` naive; it is UTC, not node-local. Reading it as
    node-local shifts every recording by the host offset and files the ones near
    midnight on the wrong day.
    """

    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(zone).date()


async def _clear_timeline_state(database: Any, user_ids: tuple[str, ...]) -> int:
    """Drop the derived timeline so analysis starts from evidence, not prior episodes.

    Runs, days, and episodes are all regenerable. Deleting them is what makes the
    rebuild honest: a surviving day carries ``memory_state: written``, which is exactly
    the latch that would skip it, and a surviving episode is offered back to the agent
    as prior art so it would reproduce the boundaries being replaced.
    """

    deleted = 0
    query = {"user_id": {"$in": list(user_ids)}}
    for name in ("timeline_episodes", "timeline_days", "timeline_analysis_runs"):
        result = await database[name].delete_many(query)
        deleted += int(result.deleted_count)
    return deleted


def _enqueue_timeline_rebuild(
    day: RebuildDay, *, run_id: str, sequence: int, depends_on: Optional[str]
) -> Job:
    """Analysis and the day write, chained one day at a time.

    Serial by construction: the write agent takes the per-user vault lock, so parallel
    days would queue on it anyway while holding worker slots.
    """

    # Imported here to break the circular import: the job module reaches back into
    # this package through the timeline memory service.
    from advanced_omi_backend.workers.timeline_jobs import rebuild_timeline_day_job

    dependency = Dependency(jobs=depends_on, allow_failure=True) if depends_on else None
    return memory_queue.enqueue(
        rebuild_timeline_day_job,
        day.user_id,
        day.local_date.isoformat(),
        day.timezone,
        job_timeout=TIMELINE_REBUILD_JOB_TIMEOUT,
        result_ttl=JOB_RESULT_TTL,
        job_id=f"timeline_rebuild_{run_id}_{sequence}_{day.local_date.isoformat()}",
        description=f"Rebuild timeline for {day.local_date.isoformat()}",
        depends_on=dependency,
        meta={"local_date": day.local_date.isoformat(), "trigger": "timeline_rebuild"},
    )


async def _reset_active_versions_to_asr(
    database: Any, conversations: Iterable[RebuildConversation]
) -> None:
    """Point every conversation back at its ASR transcript before re-diarizing.

    Downstream work is allowed to continue after a failed speaker job. Resetting the
    pointer first guarantees that continuation reads the clean ASR source, never the
    previously generated (and potentially polluted) speaker version.
    """

    for item in conversations:
        if (
            item.active_transcript_version_id
            and item.active_transcript_version_id != item.transcript_version_id
        ):
            await database["conversations"].update_one(
                {"conversation_id": item.conversation_id},
                {"$set": {"active_transcript_version": item.transcript_version_id}},
            )


def _active_rebuild_jobs(conversation_ids: set[str]) -> list[str]:
    job_ids: set[str] = set()
    for queue in (transcription_queue, memory_queue, default_queue):
        job_ids.update(queue.get_job_ids())
        for registry in (
            queue.started_job_registry,
            queue.deferred_job_registry,
            queue.scheduled_job_registry,
        ):
            job_ids.update(registry.get_job_ids())

    active: list[str] = []
    for job_id in job_ids:
        try:
            job = Job.fetch(job_id, connection=memory_queue.connection)
        except NoSuchJobError:
            continue
        if (job.meta or {}).get("conversation_id") in conversation_ids:
            active.append(job_id)
    return sorted(active)


def _enqueue_speaker_rebuild(
    item: RebuildConversation,
    *,
    run_id: str,
    sequence: int,
    depends_on: Optional[str],
) -> Job:
    """Create a speaker version from the imported active transcript."""
    dependency = Dependency(jobs=depends_on, allow_failure=True) if depends_on else None
    target_version_id = str(uuid.uuid4())
    return transcription_queue.enqueue(
        recognise_speakers_job,
        item.conversation_id,
        target_version_id,
        "",
        None,
        item.transcript_version_id,
        job_timeout=1200,
        result_ttl=JOB_RESULT_TTL,
        job_id=(f"speaker_rebuild_{run_id}_{sequence}_" f"{item.conversation_id[:12]}"),
        description=f"Rebuild speakers for {item.conversation_id[:8]}",
        **post_conv_enqueue_kwargs(
            "speaker",
            {
                "conversation_id": item.conversation_id,
                "source_version_id": item.transcript_version_id,
                "version_id": target_version_id,
                "trigger": "archive_rebuild",
            },
            depends_on=dependency,
        ),
    )


def _backup_user_vaults(
    data_dir: Path, user_ids: tuple[str, ...], backup_dir: Path
) -> Optional[Path]:
    vaults: list[Path] = []
    for root_name in VAULT_ROOTS:
        for user_id in user_ids:
            user_root = data_dir / root_name / user_id
            if user_root.is_dir():
                vaults.append(user_root)
    if not vaults:
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    final_path = backup_dir / f"memory_vault_{timestamp}.tar.gz"
    temp_path = final_path.with_name(f".{final_path.name}.partial-{os.getpid()}")
    try:
        with tarfile.open(temp_path, mode="w:gz") as archive:
            for vault in vaults:
                archive.add(vault, arcname=vault.relative_to(data_dir).as_posix())
        temp_path.replace(final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return final_path


async def execute_memory_rebuild(
    database: Any,
    plan: RebuildPlan,
    *,
    data_dir: Path,
    backup_dir: Optional[Path] = None,
    from_stage: RebuildStage = RebuildStage.MEMORY,
) -> RebuildResult:
    """Clear derived memory state and enqueue ordered replay chains per user."""
    if not plan.user_ids:
        raise MemoryRebuildError("No users matched the rebuild request")
    if not plan.conversations:
        raise MemoryRebuildError("No conversations with active transcripts matched")

    conversation_ids = {item.conversation_id for item in plan.conversations}
    active_jobs = _active_rebuild_jobs(conversation_ids)
    if active_jobs:
        preview = ", ".join(active_jobs[:5])
        raise MemoryRebuildError(
            "Existing speaker or memory jobs target this rebuild set. Wait for them "
            "to finish or "
            f"stop/cancel them first: {preview}"
        )

    vault_backup = None
    if backup_dir is not None:
        vault_backup = _backup_user_vaults(data_dir, plan.user_ids, backup_dir)

    deleted_files = 0
    for root_name in VAULT_ROOTS:
        for user_id in plan.user_ids:
            deleted_files += clear_vault_contents(data_dir / root_name / user_id)

    audit_result = await database["memory_audit"].delete_many(
        {"user_id": {"$in": list(plan.user_ids)}}
    )
    deleted_audit_entries = int(audit_result.deleted_count)

    if from_stage in TIMELINE_STAGES:
        diarize = from_stage is RebuildStage.TIMELINE
        deleted_timeline = await _clear_timeline_state(database, plan.user_ids)
        days = await build_timeline_days(database, plan.user_ids)
        if not days:
            raise MemoryRebuildError(
                "No captured audio found for these users, so there are no days to "
                "rebuild a timeline from"
            )
        run_id = uuid.uuid4().hex[:12]
        # Speakers first, and the days wait for them: an episode's transcript is the
        # speaker-labelled one, so segmenting a day whose recordings are still raw ASR
        # decides its bounds -- and writes its memory -- from text with no speakers in it.
        # DAYS skips this: it keeps the speaker layer that is already active, which is
        # exactly the transcript the day pass wants to read.
        if diarize:
            await _reset_active_versions_to_asr(database, plan.conversations)
        speaker_jobs: list[str] = []
        skipped_speaker_conversations: list[str] = []
        timeline_jobs: list[str] = []
        by_user: dict[str, list[RebuildConversation]] = defaultdict(list)
        for item in plan.conversations:
            by_user[item.user_id].append(item)
        by_user_days: dict[str, list[RebuildDay]] = defaultdict(list)
        for day in days:
            by_user_days[day.user_id].append(day)

        for user_id in plan.user_ids:
            # Speaker jobs fan out. They write only their own conversation's transcript,
            # so nothing orders them against each other, and chaining hundreds of them
            # would serialize a night's work behind one worker for no benefit.
            user_speaker_jobs: list[str] = []
            for sequence, item in enumerate(by_user[user_id] if diarize else [], 1):
                if not item.has_audio:
                    logger.warning(
                        "Speaker rebuild skipped conversation %s: no stored audio",
                        item.conversation_id,
                    )
                    skipped_speaker_conversations.append(item.conversation_id)
                    continue
                job = _enqueue_speaker_rebuild(
                    item, run_id=run_id, sequence=sequence, depends_on=None
                )
                user_speaker_jobs.append(job.id)
            speaker_jobs.extend(user_speaker_jobs)

            # Days do not: each one's write takes this user's vault lock, and the first
            # cannot start until every recording it might cite has been diarized.
            dependency: Any = user_speaker_jobs or None
            for sequence, day in enumerate(by_user_days[user_id], start=1):
                job = _enqueue_timeline_rebuild(
                    day, run_id=run_id, sequence=sequence, depends_on=dependency
                )
                timeline_jobs.append(job.id)
                dependency = job.id
        return RebuildResult(
            run_id=run_id,
            jobs=tuple((*speaker_jobs, *timeline_jobs)),
            speaker_jobs=tuple(speaker_jobs),
            skipped_speaker_conversations=tuple(skipped_speaker_conversations),
            # Deliberately empty: the day pass is the whole vault write for this stage.
            # Running the per-conversation path alongside it would record the same
            # audio twice, under the container boundaries the rebuild is replacing.
            memory_jobs=(),
            from_stage=from_stage,
            user_ids=plan.user_ids,
            deleted_vault_files=deleted_files,
            deleted_audit_entries=deleted_audit_entries,
            vault_backup=vault_backup,
            timeline_jobs=tuple(timeline_jobs),
            timeline_days=days,
            deleted_timeline_documents=deleted_timeline,
        )

    if from_stage is RebuildStage.SPEAKERS:
        await _reset_active_versions_to_asr(database, plan.conversations)

    run_id = uuid.uuid4().hex[:12]
    by_user: dict[str, list[RebuildConversation]] = defaultdict(list)
    for item in plan.conversations:
        by_user[item.user_id].append(item)

    speaker_jobs: list[str] = []
    skipped_speaker_conversations: list[str] = []
    memory_jobs: list[str] = []
    for user_id in plan.user_ids:
        user_conversations = by_user[user_id]
        memory_dependency = None
        if from_stage is RebuildStage.SPEAKERS:
            speaker_dependency = None
            speaker_conversations = [
                item for item in user_conversations if item.has_audio
            ]
            skipped = [item for item in user_conversations if not item.has_audio]
            for item in skipped:
                logger.warning(
                    "Speaker rebuild skipped conversation %s: no stored audio chunks",
                    item.conversation_id,
                )
                skipped_speaker_conversations.append(item.conversation_id)
            for sequence, item in enumerate(speaker_conversations, start=1):
                speaker_job = _enqueue_speaker_rebuild(
                    item,
                    run_id=run_id,
                    sequence=sequence,
                    depends_on=speaker_dependency,
                )
                speaker_jobs.append(speaker_job.id)
                speaker_dependency = speaker_job.id
            if speaker_dependency:
                memory_dependency = Dependency(
                    jobs=speaker_dependency,
                    allow_failure=True,
                )

        memory_conversations = [
            item for item in user_conversations if not item.memory_excluded
        ]
        for sequence, item in enumerate(memory_conversations, start=1):
            job = enqueue_memory_processing(
                item.conversation_id,
                cause=MemoryCause.MEMORY_REBUILD,
                strategy=UpdateStrategy.FULL,
                depends_on=memory_dependency,
                job_timeout=MEMORY_REBUILD_JOB_TIMEOUT,
                job_id=(
                    f"memory_rebuild_{run_id}_{sequence}_"
                    f"{item.conversation_id[:12]}"
                ),
            )
            memory_jobs.append(job.id)
            memory_dependency = job

    jobs = tuple((*speaker_jobs, *memory_jobs))

    return RebuildResult(
        run_id=run_id,
        jobs=jobs,
        speaker_jobs=tuple(speaker_jobs),
        skipped_speaker_conversations=tuple(skipped_speaker_conversations),
        memory_jobs=tuple(memory_jobs),
        from_stage=from_stage,
        user_ids=plan.user_ids,
        deleted_vault_files=deleted_files,
        deleted_audit_entries=deleted_audit_entries,
        vault_backup=vault_backup,
    )
