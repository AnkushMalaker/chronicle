#!/usr/bin/env python3
"""Run fresh speaker recognition over every active audio conversation.

This intentionally calls the production speaker job directly instead of the public
reprocess endpoint. The endpoint also creates memory and title jobs; a corpus scan only
needs speaker work and must not flood unrelated queues.

One conversation runs at a time by default. The speaker service processes bounded
neural windows concurrently *inside* one request, but serializes separate requests to
keep VRAM bounded. Outer concurrency therefore cannot increase GPU throughput; it only
stacks whole-recording WAV downloads and temporary files (including the ten-hour
recording in this corpus). An explicit higher value remains available for a deployment
whose provider can truly process separate requests concurrently.

    python src/scripts/reprocess_speakers_corpus.py             # dry run
    python src/scripts/reprocess_speakers_corpus.py --apply
    python src/scripts/reprocess_speakers_corpus.py --apply --max-duration 1200
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from advanced_omi_backend.database import get_database
from advanced_omi_backend.models.job import _ensure_beanie_initialized
from advanced_omi_backend.utils.segment_utils import classify_segment_text
from advanced_omi_backend.workers.speaker_jobs import recognise_speakers_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("reprocess_speakers_corpus")
# The production job is intentionally chatty per segment. Keep the corpus monitor at
# conversation-level progress while preserving warnings and errors from the pipeline.
logging.getLogger("advanced_omi_backend").setLevel(logging.WARNING)
logging.getLogger("rq").setLevel(logging.WARNING)

_recognise = recognise_speakers_job.__wrapped__

_UNKNOWN_SPEAKER_RE = re.compile(
    r"^(?:unknown(?:[ _-]*speaker)?(?:[ _-]*\d+)?|"
    r"speaker(?:[ _-]*\d+)?|noise|background(?: speech)?)$",
    re.IGNORECASE,
)


def _active_version(document: dict[str, Any]) -> dict[str, Any] | None:
    versions = document.get("transcript_versions") or []
    active_id = document.get("active_transcript_version")
    return next(
        (version for version in versions if version.get("version_id") == active_id),
        versions[-1] if versions else None,
    )


def _speaker_source_version(document: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve an active speaker projection back to its immutable ASR source."""

    current = _active_version(document)
    if current is None:
        return None
    versions = {
        version.get("version_id"): version
        for version in document.get("transcript_versions") or []
        if version.get("version_id")
    }
    visited: set[str] = set()
    while (current.get("metadata") or {}).get("reprocessing_type") == (
        "speaker_diarization"
    ):
        current_id = current.get("version_id")
        if not current_id or current_id in visited:
            return None
        visited.add(current_id)
        source_id = (current.get("metadata") or {}).get("source_version_id")
        current = versions.get(source_id)
        if current is None:
            return None
    return current


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_speaker_work(version: dict[str, Any]) -> bool:
    segments = version.get("segments") or []
    if segments:
        return any(
            segment.get("segment_type", "speech") == "speech"
            or classify_segment_text(segment.get("text", "")) == "speech"
            for segment in segments
        )
    return bool(version.get("words"))


def _speaker_result_status(result: dict[str, Any]) -> tuple[bool, str | None]:
    """Require evidence that speaker work ran; a healthy process is not enough."""
    if result.get("speaker_recognition_enabled") is False:
        return False, "Speaker recognition is disabled"
    if result.get("speaker_service_unavailable"):
        return False, str(result.get("skip_reason") or "Speaker service unavailable")
    if not result.get("success"):
        return False, str(
            result.get("error") or result.get("skip_reason") or "Unknown error"
        )
    if int(result.get("segment_count", 0) or 0) < 1:
        return False, "Speaker recognition completed without processing segments"
    return True, None


def _word_key(word: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Stable identity for a provider word copied into projected turns."""

    return (word.get("start"), word.get("end"), word.get("word"))


def _projection_metrics(
    source: dict[str, Any],
    projection: dict[str, Any],
    *,
    duration: float,
) -> dict[str, int | bool]:
    """Measure the lossless/exclusive invariants of one speaker projection."""

    source_words = source.get("words") or []
    projected_words = projection.get("words") or []
    source_counts = Counter(_word_key(word) for word in source_words)
    assigned_counts: Counter = Counter()
    for segment in projection.get("segments") or []:
        assigned_counts.update(_word_key(word) for word in segment.get("words") or [])

    duplicate_word_occurrences = sum(
        max(0, count - source_counts.get(key, 0))
        for key, count in assigned_counts.items()
        if key in source_counts
    )
    missing_words = sum(
        max(0, count - assigned_counts.get(key, 0))
        for key, count in source_counts.items()
    )
    extra_words = sum(
        max(0, count - source_counts.get(key, 0))
        for key, count in assigned_counts.items()
        if key not in source_counts
    )

    overlapping_segments = 0
    invalid_segment_bounds = 0
    previous_end = 0.0
    for segment in sorted(
        projection.get("segments") or [],
        key=lambda item: (float(item.get("start", 0.0)), float(item.get("end", 0.0))),
    ):
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", 0.0))
        segment_type = str(segment.get("segment_type") or "speech")
        point_boundary = segment_type in {"event", "note"} and abs(end - start) <= 1e-6
        if (
            start < 0
            or end < start
            or (end <= start and not point_boundary)
            or end > duration + 1e-3
        ):
            invalid_segment_bounds += 1
        if end > start and start < previous_end - 1e-6:
            overlapping_segments += 1
        previous_end = max(previous_end, end)

    return {
        "source_words": len(source_words),
        "assigned_word_occurrences": sum(assigned_counts.values()),
        "duplicate_word_occurrences": duplicate_word_occurrences,
        "missing_words": missing_words,
        "extra_words": extra_words,
        "overlapping_segments": overlapping_segments,
        "invalid_segment_bounds": invalid_segment_bounds,
        "words_preserved": projected_words == source_words,
    }


def _identity_metrics(
    version: dict[str, Any],
) -> tuple[Counter, Counter, Counter, Counter]:
    """Summarize automatic IDs and visible labels independently of vault notes.

    ``People/*.md`` is durable semantic memory, not the speaker gallery and not an
    inventory of successful identifications. Keeping this measurement next to the
    projection validator prevents a sparse vault from being mistaken for a failed
    speaker pass. ``identified_as`` is the model result; ``speaker`` may instead be a
    preserved human correction, so they must never be counted as the same evidence.
    """

    totals: Counter = Counter()
    visible_names: Counter = Counter()
    automatic_names: Counter = Counter()
    visible_without_model_id: Counter = Counter()
    for segment in version.get("segments") or []:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if (
            segment.get("segment_type", "speech") != "speech"
            and classify_segment_text(text) != "speech"
        ):
            continue
        try:
            duration = max(
                0.0,
                float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)),
            )
        except (TypeError, ValueError):
            duration = 0.0
        automatic_label = str(segment.get("identified_as") or "").strip()
        visible_label = str(segment.get("speaker") or "").strip()
        label = automatic_label or visible_label
        is_unknown = not label or bool(_UNKNOWN_SPEAKER_RE.fullmatch(label))
        prefix = "unknown" if is_unknown else "named"
        totals[f"{prefix}_segments"] += 1
        totals[f"{prefix}_duration_ms"] += round(duration * 1000)
        if not is_unknown:
            visible_names[label] += 1
        automatic_is_name = automatic_label and not bool(
            _UNKNOWN_SPEAKER_RE.fullmatch(automatic_label)
        )
        if automatic_is_name:
            totals["automatically_identified_segments"] += 1
            totals["automatically_identified_duration_ms"] += round(duration * 1000)
            automatic_names[automatic_label] += 1
        elif not is_unknown:
            totals["visible_named_without_model_id_segments"] += 1
            totals["visible_named_without_model_id_duration_ms"] += round(
                duration * 1000
            )
            visible_without_model_id[label] += 1
    return totals, visible_names, automatic_names, visible_without_model_id


async def _audit_identity_coverage(
    database: Any,
    targets: list[dict[str, str]],
    *,
    vault_root: Path | None = None,
) -> dict[str, Any]:
    """Report recognized identities separately from optional semantic person notes."""

    totals: Counter = Counter()
    visible_names: Counter = Counter()
    automatic_names: Counter = Counter()
    visible_without_model_id: Counter = Counter()
    for target in targets:
        document = await database["conversations"].find_one(
            {"conversation_id": target["conversation_id"]},
            projection={
                "active_transcript_version": 1,
                "transcript_versions": 1,
            },
        )
        if document is None:
            continue
        active = _active_version(document)
        if active is None:
            continue
        (
            recording_totals,
            recording_visible_names,
            recording_automatic_names,
            recording_visible_without_model_id,
        ) = _identity_metrics(active)
        totals.update(recording_totals)
        visible_names.update(recording_visible_names)
        automatic_names.update(recording_automatic_names)
        visible_without_model_id.update(recording_visible_without_model_id)
        totals["recordings"] += 1
        if recording_totals["named_segments"]:
            totals["recordings_with_names"] += 1
        elif recording_totals["unknown_segments"]:
            totals["recordings_only_unknown"] += 1

    named_duration = totals["named_duration_ms"] / 1000.0
    unknown_duration = totals["unknown_duration_ms"] / 1000.0
    automatically_identified_duration = (
        totals["automatically_identified_duration_ms"] / 1000.0
    )
    visible_without_model_id_duration = (
        totals["visible_named_without_model_id_duration_ms"] / 1000.0
    )
    speech_duration = named_duration + unknown_duration
    result: dict[str, Any] = {
        "recordings": totals["recordings"],
        "recordings_with_names": totals["recordings_with_names"],
        "recordings_only_unknown": totals["recordings_only_unknown"],
        "named_segments": totals["named_segments"],
        "unknown_segments": totals["unknown_segments"],
        "named_duration_seconds": round(named_duration, 3),
        "unknown_duration_seconds": round(unknown_duration, 3),
        "named_duration_fraction": (
            round(named_duration / speech_duration, 6) if speech_duration else 0.0
        ),
        "automatically_identified_segments": totals[
            "automatically_identified_segments"
        ],
        "automatically_identified_duration_seconds": round(
            automatically_identified_duration, 3
        ),
        "automatically_identified_names": sorted(automatic_names, key=str.casefold),
        "automatically_identified_name_count": len(automatic_names),
        "visible_named_without_model_id_segments": totals[
            "visible_named_without_model_id_segments"
        ],
        "visible_named_without_model_id_duration_seconds": round(
            visible_without_model_id_duration, 3
        ),
        "visible_names_without_model_identity": sorted(
            visible_without_model_id, key=str.casefold
        ),
        "visible_names": sorted(visible_names, key=str.casefold),
        "visible_name_count": len(visible_names),
    }
    if vault_root is not None:
        people_dir = vault_root / "People"
        vault_people = sorted(
            (path.stem for path in people_dir.glob("*.md")), key=str.casefold
        )
        people_by_casefold = {name.casefold() for name in vault_people}
        result.update(
            {
                "vault_people": vault_people,
                "vault_people_count": len(vault_people),
                "recognized_without_person_note": sorted(
                    (
                        name
                        for name in automatic_names
                        if name.casefold() not in people_by_casefold
                    ),
                    key=str.casefold,
                ),
            }
        )
    return result


async def _select(
    database: Any,
    ids: list[str],
    skip_speaker_since: datetime | None,
    min_duration: float = 0.0,
    max_duration: float | None = None,
    missing_pyannote: bool = False,
) -> list[dict[str, str]]:
    duration_query: dict[str, float] = {"$gt": min_duration}
    if max_duration is not None:
        duration_query["$lte"] = max_duration
    query: dict[str, Any] = {
        "deleted": {"$ne": True},
        "memory_excluded": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
        "audio_total_duration": duration_query,
        "active_transcript_version": {"$ne": None},
    }
    if ids:
        query["conversation_id"] = {"$in": ids}
    cursor = database["conversations"].find(
        query,
        projection={
            "conversation_id": 1,
            "active_transcript_version": 1,
            "transcript_versions.version_id": 1,
            "transcript_versions.transcript": 1,
            "transcript_versions.words": 1,
            "transcript_versions.segments": 1,
            "transcript_versions.diarization_source": 1,
            "transcript_versions.created_at": 1,
            "transcript_versions.metadata": 1,
            "created_at": 1,
        },
    )
    targets: list[dict[str, str]] = []
    async for document in cursor:
        active_version = _active_version(document)
        version = _speaker_source_version(document)
        if not version or not (version.get("transcript") or "").strip():
            continue
        if not _has_speaker_work(version):
            continue
        if (
            missing_pyannote
            and active_version is not None
            and active_version.get("diarization_source") == "pyannote"
            and (active_version.get("metadata") or {}).get("diarization_artifact_id")
        ):
            continue
        if (
            skip_speaker_since is not None
            and active_version is not None
            and (active_version.get("metadata") or {}).get("reprocessing_type")
            == "speaker_diarization"
            and active_version.get("created_at") is not None
            and _as_utc(active_version["created_at"]) >= skip_speaker_since
        ):
            continue
        targets.append(
            {
                "conversation_id": document["conversation_id"],
                "source_version_id": version["version_id"],
                "created_at": str(document.get("created_at") or ""),
            }
        )
    targets.sort(key=lambda target: target["created_at"])
    return targets


async def _validate_targets(
    database: Any, targets: list[dict[str, str]]
) -> tuple[Counter, list[str]]:
    """Validate active speaker projections and their immutable artifact lineage."""

    stats: Counter = Counter()
    issues: list[str] = []
    for target in targets:
        conversation_id = target["conversation_id"]
        document = await database["conversations"].find_one(
            {"conversation_id": conversation_id},
            projection={
                "conversation_id": 1,
                "audio_total_duration": 1,
                "active_transcript_revision_id": 1,
                "active_transcript_version": 1,
                "transcript_versions": 1,
            },
        )
        if document is None:
            issues.append(f"{conversation_id}: conversation disappeared")
            continue
        active = _active_version(document)
        source = _speaker_source_version(document)
        if active is None or source is None:
            issues.append(
                f"{conversation_id}: missing active/source transcript version"
            )
            continue
        metadata = active.get("metadata") or {}
        in_place_projection = metadata.get("reprocessing_type") != "speaker_diarization"
        if in_place_projection:
            # Initial ingestion deliberately refines the just-created provider version
            # in place. Its pre-speaker state still exists as an immutable standalone
            # revision, so validate against that rather than pretending the active
            # projection is its own source. Corpus/manual reprocessing uses the separate
            # embedded source + derived-version shape handled below.
            source_revision = await database[
                "conversation_transcript_revisions"
            ].find_one(
                {
                    "retry_key": (
                        f"transcript-projection:{conversation_id}:"
                        f"{active.get('version_id')}"
                    )
                }
            )
            if (
                not (metadata.get("speaker_recognition") or {}).get("enabled")
                or not metadata.get("diarization_artifact_id")
                or source_revision is None
            ):
                issues.append(
                    f"{conversation_id}: active version is not a speaker projection"
                )
                continue
            source_metadata = dict(source_revision.get("metadata") or {})
            source_metadata["transcript_artifact_ids"] = list(
                source_revision.get("transcript_artifact_ids") or []
            )
            source = {
                "version_id": active.get("version_id"),
                "transcript": source_revision.get("transcript") or "",
                "words": source_revision.get("words") or [],
                "segments": source_revision.get("segments") or [],
                "provider": source_revision.get("provider"),
                "model": source_revision.get("model"),
                "diarization_source": source_revision.get("diarization_source"),
                "metadata": source_metadata,
            }
            stats["in_place_projections"] += 1
        elif metadata.get("source_version_id") != source.get("version_id"):
            issues.append(
                f"{conversation_id}: projection points to another derived version"
            )
        if active.get("transcript") != source.get("transcript"):
            issues.append(f"{conversation_id}: projection changed ASR transcript text")
        if active.get("provider") != source.get("provider"):
            issues.append(f"{conversation_id}: projection changed ASR provider")
        if active.get("model") != source.get("model"):
            issues.append(f"{conversation_id}: projection changed ASR model")
        active_diarization_source = active.get("diarization_source")
        if active_diarization_source not in {
            "pyannote",
            "word_timeline_fallback",
        }:
            issues.append(
                f"{conversation_id}: active diarization source is neither pyannote "
                "nor the explicit word-timeline fallback"
            )
        elif active_diarization_source == "word_timeline_fallback":
            fallback = metadata.get("diarization_fallback") or {}
            if fallback != {"mode": "word_timeline", "reason": "pyannote_empty"}:
                issues.append(
                    f"{conversation_id}: word-timeline fallback provenance is incomplete"
                )
            stats["word_timeline_fallbacks"] += 1
        else:
            stats["pyannote_projections"] += 1

        metrics = _projection_metrics(
            source,
            active,
            duration=float(document.get("audio_total_duration") or 0.0),
        )
        stats["source_words"] += int(metrics["source_words"])
        stats["assigned_word_occurrences"] += int(metrics["assigned_word_occurrences"])
        stats["missing_words"] += int(metrics["missing_words"])
        for metric in (
            "duplicate_word_occurrences",
            "missing_words",
            "extra_words",
            "overlapping_segments",
            "invalid_segment_bounds",
        ):
            if metrics[metric]:
                issues.append(f"{conversation_id}: {metric}={metrics[metric]}")
        if not metrics["words_preserved"]:
            issues.append(f"{conversation_id}: projection changed the ASR word array")

        artifact_id = metadata.get("diarization_artifact_id")
        artifact = (
            await database["diarization_artifacts"].find_one(
                {"artifact_id": artifact_id}
            )
            if artifact_id
            else None
        )
        if artifact is None:
            issues.append(f"{conversation_id}: missing diarization artifact")
            continue
        configuration = artifact.get("configuration") or {}
        required_configuration = {
            "requested_source": "pyannote",
            "ran_pyannote_segmentation": True,
            "neural_window_ceiling_seconds": 1200,
            "min_duration": 0,
            "min_duration_off": 0,
            "min_speakers": None,
            "max_speakers": None,
        }
        for key, expected in required_configuration.items():
            if configuration.get(key) != expected:
                issues.append(
                    f"{conversation_id}: artifact {key}="
                    f"{configuration.get(key)!r}, expected {expected!r}"
                )
        if active_diarization_source == "word_timeline_fallback":
            if artifact.get("provider") != "word_timeline_fallback":
                issues.append(
                    f"{conversation_id}: fallback artifact provider is not explicit"
                )
            if configuration.get("pyannote_returned_turns") is not False:
                issues.append(
                    f"{conversation_id}: fallback does not record empty pyannote turns"
                )
            if configuration.get("fallback_mode") != "word_timeline":
                issues.append(
                    f"{conversation_id}: fallback artifact mode is not word_timeline"
                )
        elif artifact.get("provider") != "pyannote":
            issues.append(f"{conversation_id}: diarization artifact is not pyannote")
        if not artifact.get("turns"):
            issues.append(f"{conversation_id}: diarization artifact has no turns")

        retry_key = f"speaker-projection:{conversation_id}:{active['version_id']}"
        revision = await database["conversation_transcript_revisions"].find_one(
            {"retry_key": retry_key}
        )
        if revision is None:
            issues.append(f"{conversation_id}: missing transcript revision")
            continue
        if revision.get("diarization_artifact_ids") != [artifact_id]:
            issues.append(f"{conversation_id}: revision diarization link mismatch")

        source_metadata = source.get("metadata") or {}
        expected_transcript_ids = list(
            dict.fromkeys(source_metadata.get("transcript_artifact_ids") or [])
        )
        if not expected_transcript_ids:
            source_revision = await database[
                "conversation_transcript_revisions"
            ].find_one(
                {
                    "conversation_id": conversation_id,
                    "metadata.source_version_id": source.get("version_id"),
                    "transcript_artifact_ids.0": {"$exists": True},
                },
                projection={"transcript_artifact_ids": 1},
            )
            if source_revision:
                expected_transcript_ids = list(
                    dict.fromkeys(source_revision.get("transcript_artifact_ids") or [])
                )
        active_transcript_ids = list(
            dict.fromkeys(metadata.get("transcript_artifact_ids") or [])
        )
        revision_transcript_ids = list(
            dict.fromkeys(revision.get("transcript_artifact_ids") or [])
        )
        if expected_transcript_ids:
            stats["raw_transcript_lineages"] += 1
            if active_transcript_ids != expected_transcript_ids:
                issues.append(f"{conversation_id}: active transcript link mismatch")
            if revision_transcript_ids != expected_transcript_ids:
                issues.append(f"{conversation_id}: revision transcript link mismatch")
        else:
            stats["revision_only_lineages"] += 1
            if active_transcript_ids or revision_transcript_ids:
                issues.append(
                    f"{conversation_id}: fabricated transcript artifact lineage"
                )
        stats["validated"] += 1

    return stats, issues


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate active projections/artifact lineage without processing audio",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        help=(
            "Optional user vault root. With --validate, report semantic People notes "
            "separately from names recognized in transcripts."
        ),
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--conversation-id", action="append", default=[])
    parser.add_argument(
        "--missing-pyannote",
        action="store_true",
        help="Only process active audio whose current projection lacks Pyannote evidence",
    )
    parser.add_argument(
        "--min-duration",
        type=float,
        default=0.0,
        help="Only process recordings longer than this many seconds",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=None,
        help="Only process recordings no longer than this many seconds",
    )
    parser.add_argument(
        "--skip-speaker-since",
        help="Resume marker: skip active speaker versions created at/after this ISO time",
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.min_duration < 0:
        parser.error("--min-duration must be non-negative")
    if args.max_duration is not None and args.max_duration <= 0:
        parser.error("--max-duration must be positive")
    if args.max_duration is not None and args.max_duration <= args.min_duration:
        parser.error("--max-duration must be greater than --min-duration")

    database = get_database()
    await database.command("ping")
    await _ensure_beanie_initialized()
    skip_speaker_since = None
    if args.skip_speaker_since:
        skip_speaker_since = _as_utc(
            datetime.fromisoformat(args.skip_speaker_since.replace("Z", "+00:00"))
        )
    targets = await _select(
        database,
        args.conversation_id,
        skip_speaker_since,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        missing_pyannote=args.missing_pyannote,
    )
    if args.limit:
        targets = targets[: args.limit]
    log.info(
        "targets=%d outer_concurrency=%d speaker_only=true",
        len(targets),
        args.concurrency,
    )
    if args.validate:
        stats, issues = await _validate_targets(database, targets)
        log.info("VALIDATION stats=%s issues=%d", dict(stats), len(issues))
        identity_coverage = await _audit_identity_coverage(
            database,
            targets,
            vault_root=args.vault_root,
        )
        log.info("IDENTITY_COVERAGE %s", identity_coverage)
        invalid_conversation_ids = sorted(
            {issue.split(":", 1)[0] for issue in issues if ":" in issue}
        )
        log.info(
            "INVALID_CONVERSATIONS count=%d ids=%s",
            len(invalid_conversation_ids),
            ",".join(invalid_conversation_ids),
        )
        for issue in issues[:100]:
            log.error("INVALID %s", issue)
        if len(issues) > 100:
            log.error("INVALID ... and %d more", len(issues) - 100)
        if issues:
            raise SystemExit(1)
        return
    if not args.apply:
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    stats: Counter = Counter()
    started = time.monotonic()

    async def process(position: int, target: dict[str, str]) -> None:
        async with semaphore:
            conversation_id = target["conversation_id"]
            try:
                result = await _recognise(
                    conversation_id,
                    str(uuid.uuid4()),
                    source_version_id=target["source_version_id"],
                )
                completed_work, result_error = _speaker_result_status(result)
                if completed_work:
                    stats["ok"] += 1
                    stats["processed_segments"] += int(
                        result.get("segment_count", 0) or 0
                    )
                    stats["identified_speakers"] += len(
                        result.get("identified_speakers") or []
                    )
                else:
                    stats["failed"] += 1
                    log.error(
                        "FAIL %s: %s",
                        conversation_id,
                        result_error[:240] if result_error else "Unknown error",
                    )
            except Exception as error:  # noqa: BLE001 - finish the rest of the corpus
                stats["failed"] += 1
                log.error("FAIL %s: %s", conversation_id, str(error)[:300])
            finally:
                async with lock:
                    stats["completed"] += 1
                    completed = stats["completed"]
                    if completed % 10 == 0 or completed == len(targets):
                        elapsed = time.monotonic() - started
                        log.info(
                            "PROGRESS %d/%d %.1f%% elapsed=%.0fs rate=%.2f/min stats=%s",
                            completed,
                            len(targets),
                            completed * 100 / len(targets),
                            elapsed,
                            completed * 60 / elapsed,
                            dict(stats),
                        )

    await asyncio.gather(
        *(process(position, target) for position, target in enumerate(targets, 1))
    )
    log.info("DONE elapsed=%.1fs stats=%s", time.monotonic() - started, dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
