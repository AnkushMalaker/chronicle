"""Bulk reconstruction of derived Markdown memories from durable transcripts."""

from __future__ import annotations

import os
import tarfile
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

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
from advanced_omi_backend.workers.memory_jobs import enqueue_memory_processing
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

VAULT_ROOTS = ("conversation_docs", "memory_md")
MEMORY_REBUILD_JOB_TIMEOUT = 7200

import logging

logger = logging.getLogger(__name__)


class MemoryRebuildError(RuntimeError):
    """Raised when a clean memory rebuild cannot start safely."""


class RebuildStage(str, Enum):
    """Earliest derived-data stage to rerun during reconstruction."""

    MEMORY = "memory"
    SPEAKERS = "speakers"


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
                    if from_stage is RebuildStage.SPEAKERS
                    else document["active_transcript_version"]
                ),
                memory_excluded=document.get("memory_excluded", False) is True,
                active_transcript_version_id=document["active_transcript_version"],
            )
        )
    if from_stage is RebuildStage.SPEAKERS and conversations_list:
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

    if from_stage is RebuildStage.SPEAKERS:
        # Memory is allowed to continue after a failed speaker job. Resetting the
        # pointer first guarantees that continuation reads the clean ASR source,
        # never the previously generated (and potentially polluted) speaker version.
        for item in plan.conversations:
            if (
                item.active_transcript_version_id
                and item.active_transcript_version_id != item.transcript_version_id
            ):
                await database["conversations"].update_one(
                    {"conversation_id": item.conversation_id},
                    {"$set": {"active_transcript_version": item.transcript_version_id}},
                )

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
