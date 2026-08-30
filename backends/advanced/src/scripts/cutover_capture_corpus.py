#!/usr/bin/env python3
"""Build and validate a clean capture-owned corpus in a separate staging database.

This is the explicitly approved one-time cutover, not a runtime migration or dual-read
path.  Dry-run is the default.  ``--apply`` refuses the live database, refuses a
non-empty destination, and never deletes source data.

Example:

    MONGODB_URI=mongodb://localhost:27017 \
      uv run python src/scripts/cutover_capture_corpus.py \
      --destination-database chronicle_capture_v2_staging_20260812

Add ``--apply`` only after the dry-run report is clean.  Stop ingest writers for the
final run; the script rejects a source whose collection counts change underneath it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from advanced_omi_backend.database import MONGODB_DATABASE, mongo_client
from advanced_omi_backend.models.annotation import Annotation
from advanced_omi_backend.models.api_key import ApiKey
from advanced_omi_backend.models.audio_capture import (
    AudioCaptureSession,
    ConversationTranscriptRevision,
    DiarizationArtifact,
    TranscriptArtifact,
)
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
    PairingCode,
)
from advanced_omi_backend.models.manual_memory import ManualMemory
from advanced_omi_backend.models.memory_audit import MemoryAuditEntry
from advanced_omi_backend.models.system_event import SystemEvent
from advanced_omi_backend.models.timeline import (
    AudioEvidenceSpan,
    TimelineAnalysisRun,
    TimelineDay,
    TimelineEpisode,
)
from advanced_omi_backend.models.waveform import WaveformData
from advanced_omi_backend.services.corpus_cutover import (
    COPIED_COLLECTIONS,
    FORBIDDEN_CHUNK_FIELDS,
    FORBIDDEN_CONVERSATION_FIELDS,
    HUMAN_REFERENCE_COLLECTIONS,
    REGENERATED_COLLECTIONS,
    build_conversation_document,
    build_processing_conversation_documents,
    build_processing_lineage_catalog,
    classify_collections,
    convert_capture_chunk,
    plan_capture_corpus,
    plan_conversation_audio,
    scan_processing_conversation,
    should_materialize_conversation,
    transform_audio_evidence_span,
    transform_device_input_item,
)
from advanced_omi_backend.services.corpus_reconciliation import load_manifest
from advanced_omi_backend.users import User

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("capture-corpus-cutover")

INSERT_BATCH_SIZE = 100
PROGRESS_SECONDS = 10.0


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _report_path(destination: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        Path("/mnt/wsl/data/chronicle-cutover/reports") / f"{destination}-{stamp}.json"
    )


async def _collection_counts(database: AsyncIOMotorDatabase) -> dict[str, int]:
    names = sorted(
        name
        for name in await database.list_collection_names()
        if not name.startswith("system.")
    )
    return {name: await database[name].count_documents({}) for name in names}


async def _batched(
    cursor: AsyncIterator[dict[str, Any]], size: int
) -> AsyncIterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    async for document in cursor:
        batch.append(document)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _insert_many(
    collection: AsyncIOMotorCollection,
    documents: Iterable[dict[str, Any]],
    *,
    batch_size: int = INSERT_BATCH_SIZE,
) -> int:
    batch: list[dict[str, Any]] = []
    inserted = 0
    for document in documents:
        batch.append(document)
        if len(batch) >= batch_size:
            await collection.insert_many(batch, ordered=True)
            inserted += len(batch)
            batch = []
    if batch:
        await collection.insert_many(batch, ordered=True)
        inserted += len(batch)
    return inserted


async def _copy_collection(
    source: AsyncIOMotorDatabase,
    destination: AsyncIOMotorDatabase,
    name: str,
    canonical_by_source: Mapping[str, str] | None = None,
) -> int:
    copied = 0
    cursor = source[name].find({}).sort("_id", 1)
    async for batch in _batched(cursor, INSERT_BATCH_SIZE):
        if canonical_by_source:
            for document in batch:
                value = document.get("conversation_id")
                if isinstance(value, str):
                    document["conversation_id"] = canonical_by_source.get(value, value)
                related = document.get("related_conversation_ids")
                if isinstance(related, list):
                    document["related_conversation_ids"] = list(
                        dict.fromkeys(
                            canonical_by_source.get(value, value) for value in related
                        )
                    )
        await destination[name].insert_many(batch, ordered=True)
        copied += len(batch)
    return copied


async def _protected_conversation_ids(source: AsyncIOMotorDatabase) -> set[str]:
    protected: set[str] = set()
    source_names = set(await source.list_collection_names())
    for name in sorted(HUMAN_REFERENCE_COLLECTIONS & source_names):
        query: dict[str, Any] = {"conversation_id": {"$type": "string"}}
        if name == "background_suppressions":
            query["status"] = {"$in": ["restored", "confirmed"]}
        values = await source[name].distinct("conversation_id", query)
        protected.update(str(value) for value in values if value)
    return protected


def _recover_promoted_media(
    source: Mapping[str, Any], data_dir: Path
) -> tuple[bytes, str, str] | None:
    """Load path-only promoted media from a verified vault/archive copy."""

    promoted_path = source.get("promoted_path")
    if not promoted_path or source.get("media_data") is not None:
        return None
    relative = PurePosixPath(str(promoted_path))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(
            f"Unsafe promoted media path {promoted_path!r} on {source['_id']}"
        )
    user_id = str(source["user_id"])
    direct = [data_dir / "conversation_docs" / user_id / Path(*relative.parts)]
    archived: list[Path] = []
    backup_root = data_dir / "backups"
    if backup_root.is_dir():
        archived.extend(sorted(backup_root.glob(f"**/vault/{relative.as_posix()}")))
        archived.extend(
            sorted(
                backup_root.glob(
                    f"**/conversation_docs/{user_id}/{relative.as_posix()}"
                )
            )
        )
    candidates = [path for path in [*direct, *archived] if path.is_file()]
    if not candidates:
        raise FileNotFoundError(
            f"Promoted media {promoted_path!r} for {source['_id']} is not under {data_dir}"
        )
    expected = str(source.get("content_hash") or "").lower()
    for candidate in candidates:
        data = candidate.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not expected or digest == expected:
            content_type = mimetypes.guess_type(candidate.name)[0] or (
                "image/heic"
                if candidate.suffix.lower() in {".heic", ".heif"}
                else "application/octet-stream"
            )
            return data, candidate.name, content_type
    raise ValueError(
        f"No copy of promoted media {promoted_path!r} matches hash {expected}"
    )


def _update_fingerprint(state: dict[str, Any], document: Mapping[str, Any]) -> None:
    audio = document.get("audio_data")
    if audio is None:
        return
    raw = bytes(audio)
    chunk_digest = hashlib.sha256(
        str(document["_id"]).encode("ascii") + hashlib.sha256(raw).digest()
    ).digest()
    state["xor"] ^= int.from_bytes(chunk_digest, "big")
    state["bytes"] += len(raw)
    state["chunks"] += 1


def _fingerprint_report(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chunks": state["chunks"],
        "bytes": state["bytes"],
        "xor_sha256": f"{state['xor']:064x}",
    }


async def _destination_audio_fingerprint(
    destination: AsyncIOMotorDatabase,
) -> dict[str, Any]:
    state = {"xor": 0, "bytes": 0, "chunks": 0}
    async for document in destination["audio_chunks"].find(
        {}, {"_id": 1, "audio_data": 1}
    ):
        _update_fingerprint(state, document)
    return _fingerprint_report(state)


async def _validate_destination(
    destination: AsyncIOMotorDatabase,
    *,
    expected_audio: Mapping[str, Any],
    expected_counts: Mapping[str, int],
    expected_disabled_audio: int,
    materialized_ids: set[str],
) -> dict[str, Any]:
    validation: dict[str, Any] = {}
    validation["forbidden_chunk_fields"] = await destination[
        "audio_chunks"
    ].count_documents(
        {
            "$or": [
                {field: {"$exists": True}} for field in sorted(FORBIDDEN_CHUNK_FIELDS)
            ]
        }
    )
    validation["forbidden_conversation_fields"] = await destination[
        "conversations"
    ].count_documents(
        {
            "$or": [
                {field: {"$exists": True}}
                for field in sorted(FORBIDDEN_CONVERSATION_FIELDS)
            ]
        }
    )
    validation["chunks_missing_capture_identity"] = await destination[
        "audio_chunks"
    ].count_documents(
        {
            "$or": [
                {"user_id": {"$exists": False}},
                {"capture_source_id": {"$exists": False}},
                {"capture_session_id": {"$exists": False}},
                {"sequence": {"$exists": False}},
                {"captured_at": {"$exists": False}},
            ]
        }
    )
    validation["chunks_missing_audio_data"] = await destination[
        "audio_chunks"
    ].count_documents(
        {
            "$or": [
                {"audio_data": {"$exists": False}},
                {"audio_data": {"$type": 10}},
            ]
        }
    )

    chunk_states = {
        str(row["_id"]): bool(row.get("deleted"))
        async for row in destination["audio_chunks"].find({}, {"_id": 1, "deleted": 1})
    }
    chunk_ids = set(chunk_states)
    missing_claim_ids: set[str] = set()
    disabled_claim_ids: set[str] = set()
    claim_collections = (
        "conversations",
        "audio_evidence_spans",
        "transcript_artifacts",
        "diarization_artifacts",
    )
    for name in claim_collections:
        async for row in destination[name].find({}, {"audio_ranges.chunk_ids": 1}):
            for audio_range in row.get("audio_ranges") or []:
                missing_claim_ids.update(
                    str(chunk_id)
                    for chunk_id in audio_range.get("chunk_ids") or []
                    if str(chunk_id) not in chunk_ids
                )
                disabled_claim_ids.update(
                    str(chunk_id)
                    for chunk_id in audio_range.get("chunk_ids") or []
                    if chunk_states.get(str(chunk_id)) is True
                )
    validation["missing_claim_chunk_ids"] = sorted(missing_claim_ids)[:20]
    validation["missing_claim_chunk_count"] = len(missing_claim_ids)
    validation["disabled_claim_chunk_ids"] = sorted(disabled_claim_ids)[:20]
    validation["disabled_claim_chunk_count"] = len(disabled_claim_ids)

    session_ids = {
        str(value)
        for value in await destination["audio_capture_sessions"].distinct(
            "capture_session_id"
        )
    }
    chunk_session_ids = {
        str(value)
        for value in await destination["audio_chunks"].distinct("capture_session_id")
    }
    validation["missing_capture_session_ids"] = sorted(chunk_session_ids - session_ids)[
        :20
    ]
    validation["missing_capture_session_count"] = len(chunk_session_ids - session_ids)
    validation["disabled_audio_chunks"] = await destination[
        "audio_chunks"
    ].count_documents({"deleted": True})
    validation["expected_disabled_audio_chunks"] = expected_disabled_audio

    revision_ids = {
        str(value)
        for value in await destination["conversation_transcript_revisions"].distinct(
            "revision_id"
        )
    }
    unresolved_active_revisions: list[str] = []
    async for row in destination["conversations"].find(
        {"active_transcript_revision_id": {"$type": "string"}},
        {"conversation_id": 1, "active_transcript_revision_id": 1},
    ):
        if str(row["active_transcript_revision_id"]) not in revision_ids:
            unresolved_active_revisions.append(str(row.get("conversation_id")))
    validation["unresolved_active_revision_ids"] = unresolved_active_revisions[:20]
    validation["unresolved_active_revision_count"] = len(unresolved_active_revisions)

    unresolved_human_references: dict[str, int] = {}
    for name in (
        "annotations",
        "background_clips",
        "background_foreground_clips",
        "background_suppressions",
        "media_role_overrides",
        "source_audit_reviews",
        "speaker_label_reviews",
    ):
        if name not in await destination.list_collection_names():
            continue
        unresolved_human_references[name] = await destination[name].count_documents(
            {
                "conversation_id": {
                    "$type": "string",
                    "$nin": sorted(materialized_ids),
                }
            }
        )
    validation["unresolved_human_references"] = {
        name: count for name, count in unresolved_human_references.items() if count
    }
    validation["path_only_promoted_media"] = await destination[
        "device_input_items"
    ].count_documents(
        {
            "metadata.cutover_source_promoted_path": {"$exists": True},
            "media_data": None,
        }
    )

    destination_audio = await _destination_audio_fingerprint(destination)
    validation["audio_fingerprint"] = destination_audio
    validation["audio_matches_source"] = destination_audio == dict(expected_audio)
    validation["materialized_conversations"] = await destination[
        "conversations"
    ].count_documents({})
    validation["materialized_id_count"] = len(materialized_ids)
    validation["collection_counts"] = await _collection_counts(destination)
    validation["copied_collection_count_mismatches"] = {
        name: {
            "expected": expected,
            "actual": validation["collection_counts"].get(name, 0),
        }
        for name, expected in expected_counts.items()
        if validation["collection_counts"].get(name, 0) != expected
    }
    validation["ok"] = not any(
        (
            validation["forbidden_chunk_fields"],
            validation["forbidden_conversation_fields"],
            validation["chunks_missing_capture_identity"],
            validation["chunks_missing_audio_data"],
            validation["missing_claim_chunk_count"],
            validation["disabled_claim_chunk_count"],
            validation["missing_capture_session_count"],
            validation["disabled_audio_chunks"] != expected_disabled_audio,
            validation["unresolved_active_revision_count"],
            bool(validation["unresolved_human_references"]),
            validation["path_only_promoted_media"],
            not validation["audio_matches_source"],
            validation["materialized_conversations"] != len(materialized_ids),
            bool(validation["copied_collection_count_mismatches"]),
        )
    )
    return validation


async def _initialize_destination_models(destination: AsyncIOMotorDatabase) -> None:
    """Create the exact indexes a real backend startup will require."""

    await init_beanie(
        database=destination,
        document_models=[
            User,
            ApiKey,
            Conversation,
            AudioCaptureSession,
            AudioChunkDocument,
            TranscriptArtifact,
            DiarizationArtifact,
            ConversationTranscriptRevision,
            WaveformData,
            Annotation,
            MemoryAuditEntry,
            ManualMemory,
            SystemEvent,
            CaptureSource,
            PairingCode,
            DeviceInputItem,
            DeviceInputJob,
            AudioEvidenceSpan,
            TimelineAnalysisRun,
            TimelineEpisode,
            TimelineDay,
        ],
    )


async def _validate_transformed_models(
    destination: AsyncIOMotorDatabase,
) -> dict[str, int]:
    """Parse every transformed row through the exact model runtime will use."""

    models = (
        Conversation,
        AudioCaptureSession,
        AudioChunkDocument,
        TranscriptArtifact,
        DiarizationArtifact,
        ConversationTranscriptRevision,
        DeviceInputItem,
        AudioEvidenceSpan,
    )
    validated: dict[str, int] = {}
    for model in models:
        collection_name = model.Settings.name
        count = 0
        cursor = destination[collection_name].find({}).sort("_id", 1).batch_size(1)
        async for document in cursor:
            model.model_validate(document)
            count += 1
        validated[collection_name] = count
    return validated


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.reconciliation_manifest)
    if not manifest.get("activation_allowed"):
        raise RuntimeError("reconciliation manifest has unresolved blockers")
    canonical_by_source = manifest["canonical_by_source"]
    retired_ids = set(manifest["retired_source_ids"])
    transcript_source_by_canonical = {
        decision["canonical_id"]: decision["selected_transcript_source_id"]
        for decision in manifest.get("decisions", [])
        if decision.get("selected_transcript_source_id")
    }
    source = mongo_client[args.source_database]
    destination = mongo_client[args.destination_database]
    await source.command("ping")

    source_names = sorted(await source.list_collection_names())
    copied_names, transformed_names, regenerated_names, unknown_names = (
        classify_collections(source_names)
    )
    if unknown_names:
        raise RuntimeError(
            "Unclassified source collections; decide whether to copy, transform, or regenerate: "
            + ", ".join(sorted(unknown_names))
        )
    if args.apply and await destination.list_collection_names():
        raise RuntimeError(
            f"Destination database is not empty: {args.destination_database}"
        )

    source_counts_before = await _collection_counts(source)
    protected_ids = await _protected_conversation_ids(source)
    stats: Counter[str] = Counter()
    report: dict[str, Any] = {
        "source_database": args.source_database,
        "destination_database": args.destination_database,
        "mode": "apply" if args.apply else "dry-run",
        "started_at": datetime.now(timezone.utc),
        "source_counts_before": source_counts_before,
        "classification": {
            "copied": sorted(copied_names),
            "transformed": sorted(transformed_names),
            "regenerated": sorted(regenerated_names),
            "unknown": sorted(unknown_names),
        },
        "protected_conversation_ids": len(protected_ids),
        "reconciliation": {
            "manifest": str(args.reconciliation_manifest),
            "retired_source_ids": len(retired_ids),
            "decisions": len(manifest.get("decisions", [])),
        },
    }
    protected_retired = protected_ids & retired_ids
    if protected_retired:
        raise RuntimeError(
            "reconciliation would retire Conversations with human references: "
            + ", ".join(sorted(protected_retired))
        )

    conversation_projection = {
        "_id": 1,
        "conversation_id": 1,
        "user_id": 1,
        "client_id": 1,
        "created_at": 1,
        "deleted": 1,
        "deletion_reason": 1,
        "data_purpose": 1,
        "external_source_id": 1,
        "external_source_type": 1,
        "starred": 1,
    }
    conversations = (
        await source["conversations"]
        .find(
            {"conversation_id": {"$nin": sorted(retired_ids)}}, conversation_projection
        )
        .sort("_id", 1)
        .to_list(length=None)
    )
    meaningful_ids = {
        str(row["conversation_id"])
        async for row in source["conversations"].find(
            {
                "conversation_id": {"$nin": sorted(retired_ids)},
                "$or": [
                    {"transcript_versions.transcript": {"$regex": r"\S"}},
                    {"transcript_versions.segments.text": {"$regex": r"\S"}},
                ],
            },
            {"conversation_id": 1},
        )
    }
    for conversation in conversations:
        conversation["_has_meaningful_transcript"] = (
            str(conversation["conversation_id"]) in meaningful_ids
        )
    conversations_by_id = {
        str(conversation["conversation_id"]): conversation
        for conversation in conversations
    }
    selected_transcript_documents = {
        str(document["conversation_id"]): document
        for document in await source["conversations"]
        .find(
            {
                "conversation_id": {
                    "$in": sorted(set(transcript_source_by_canonical.values()))
                }
            }
        )
        .to_list(length=None)
    }

    def select_transcript(document: dict[str, Any]) -> dict[str, Any]:
        canonical_id = str(document["conversation_id"])
        source_id = transcript_source_by_canonical.get(canonical_id)
        selected = selected_transcript_documents.get(source_id or "")
        if selected is None or source_id == canonical_id:
            return document
        merged = dict(document)
        merged["transcript_versions"] = selected.get("transcript_versions", [])
        merged["active_transcript_version"] = selected.get("active_transcript_version")
        # Preserve semantic/capture identity from the oldest canonical record. Only
        # the cached provider output crosses from the later duplicate.
        return merged

    chunk_projection = {
        "_id": 1,
        "conversation_id": 1,
        "chunk_index": 1,
        "start_time": 1,
        "end_time": 1,
        "duration": 1,
        "captured_at": 1,
        "created_at": 1,
        "source_stream": 1,
        "source_first_message_id": 1,
        "source_last_message_id": 1,
        "sample_rate": 1,
        "channels": 1,
        "original_size": 1,
        "compressed_size": 1,
        "deleted": 1,
        "deleted_at": 1,
        "deletion_reason": 1,
    }
    chunk_metadata = (
        await source["audio_chunks"]
        .find({"conversation_id": {"$nin": sorted(retired_ids)}}, chunk_projection)
        .sort("_id", 1)
        .to_list(length=None)
    )
    chunks_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunk_metadata:
        chunks_by_conversation.setdefault(str(chunk["conversation_id"]), []).append(
            chunk
        )
    capture_plan = plan_capture_corpus(conversations_by_id, chunk_metadata)

    range_documents: dict[str, list[dict[str, Any]]] = {}
    plans_by_conversation = {}
    materialized_ids: set[str] = set()
    source_audio_state = {"xor": 0, "bytes": 0, "chunks": 0}
    last_progress = time.monotonic()
    total_conversations = len(conversations)
    stats["capture_sessions"] = len(capture_plan.capture_sessions)
    for position, conversation in enumerate(conversations, start=1):
        conversation_id = str(conversation["conversation_id"])
        chunks = chunks_by_conversation.get(conversation_id, [])
        plan = plan_conversation_audio(
            conversation, chunks, assignments=capture_plan.assignments
        )
        plans_by_conversation[conversation_id] = plan
        ranges = [item.model_dump(mode="python") for item in plan.audio_ranges]
        range_documents[conversation_id] = ranges
        stats["source_conversations"] += 1
        stats["source_chunks"] += plan.source_chunk_count
        stats["selected_claim_chunks"] += plan.claimed_chunk_count
        stats["quarantined_audio_chunks"] += len(plan.quarantined)
        stats["audio_ranges"] += len(plan.audio_ranges)
        stats[f"claim_time_basis_{plan.time_basis}"] += plan.claimed_chunk_count

        materialize = should_materialize_conversation(conversation, protected_ids)
        if materialize:
            materialized_ids.add(conversation_id)
            stats["materialized_conversations"] += 1
            if conversation.get("data_purpose") == "capture_evidence":
                stats["protected_capture_evidence_conversations"] += 1
        else:
            stats["artifact_only_groupings"] += 1

        if time.monotonic() - last_progress >= PROGRESS_SECONDS:
            logger.info(
                "conversations %d/%d | chunks %d claimed, %d disabled/ambiguous",
                position,
                total_conversations,
                stats["selected_claim_chunks"],
                stats["quarantined_audio_chunks"],
            )
            last_progress = time.monotonic()

    logger.info("Processing transcript history pass 1/2 (bounded classification)")
    scanned_dispositions = []
    scanned_conversations = 0
    last_processing_progress = time.monotonic()
    active_query = {"conversation_id": {"$nin": sorted(retired_ids)}}
    cursor = source["conversations"].find(active_query).sort("_id", 1).batch_size(1)
    async for conversation in cursor:
        conversation = select_transcript(conversation)
        conversation_id = str(conversation["conversation_id"])
        scanned_dispositions.extend(
            scan_processing_conversation(
                conversation, plans_by_conversation[conversation_id]
            )
        )
        scanned_conversations += 1
        if time.monotonic() - last_processing_progress >= PROGRESS_SECONDS:
            logger.info(
                "transcript scan %d/%d Conversations | %d versions",
                scanned_conversations,
                total_conversations,
                len(scanned_dispositions),
            )
            last_processing_progress = time.monotonic()
    catalog = build_processing_lineage_catalog(scanned_dispositions)
    stats.update(catalog.disposition_counts)
    stats["source_transcript_versions"] = len(scanned_dispositions)

    if args.apply:
        for plan in plans_by_conversation.values():
            await _insert_many(
                destination["capture_cutover_quarantine"], plan.quarantined
            )

    logger.info("Processing transcript history pass 2/2 (bounded materialization)")
    processed_conversations = 0
    last_processing_progress = time.monotonic()
    cursor = source["conversations"].find(active_query).sort("_id", 1).batch_size(1)
    async for conversation in cursor:
        conversation = select_transcript(conversation)
        conversation_id = str(conversation["conversation_id"])
        documents = build_processing_conversation_documents(
            conversation, plans_by_conversation[conversation_id], catalog
        )
        stats["quarantined_transcript_records"] += len(documents.quarantined)
        for name, amount in documents.disposition_counts.items():
            stats[name] += amount
        if args.apply:
            await _insert_many(
                destination["capture_cutover_quarantine"], documents.quarantined
            )
            await _insert_many(
                destination["transcript_artifacts"],
                documents.transcript_artifacts,
            )
            await _insert_many(
                destination["diarization_artifacts"],
                documents.diarization_artifacts,
            )
            await _insert_many(
                destination["conversation_transcript_revisions"],
                documents.revisions,
            )
            if conversation_id in materialized_ids:
                current = build_conversation_document(
                    conversation,
                    plans_by_conversation[conversation_id],
                    active_revision_id=documents.active_revision_id,
                    allowed_fields=set(Conversation.model_fields),
                )
                await destination["conversations"].insert_one(current)
        processed_conversations += 1
        if time.monotonic() - last_processing_progress >= PROGRESS_SECONDS:
            logger.info(
                "transcript materialization %d/%d Conversations",
                processed_conversations,
                total_conversations,
            )
            last_processing_progress = time.monotonic()

    if args.apply:
        logger.info(
            "Copying all %d raw audio documents byte-for-byte into %d global capture sessions",
            len(chunk_metadata),
            len(capture_plan.capture_sessions),
        )
        converted_batch: list[dict[str, Any]] = []
        raw_cursor = source["audio_chunks"].find(active_query).sort("_id", 1)
        copied_audio = 0
        last_audio_progress = time.monotonic()
        async for source_chunk in raw_cursor:
            if source_chunk.get("audio_data") is None:
                raise RuntimeError(
                    f"Raw audio chunk {source_chunk['_id']} has no audio_data"
                )
            assignment = capture_plan.assignments[str(source_chunk["_id"])]
            converted = convert_capture_chunk(source_chunk, assignment)
            _update_fingerprint(source_audio_state, converted)
            converted_batch.append(converted)
            if len(converted_batch) >= INSERT_BATCH_SIZE:
                await destination["audio_chunks"].insert_many(
                    converted_batch, ordered=True
                )
                copied_audio += len(converted_batch)
                converted_batch = []
            if time.monotonic() - last_audio_progress >= PROGRESS_SECONDS:
                logger.info(
                    "raw audio %d/%d documents copied",
                    copied_audio,
                    len(chunk_metadata),
                )
                last_audio_progress = time.monotonic()
        if converted_batch:
            await destination["audio_chunks"].insert_many(converted_batch, ordered=True)
            copied_audio += len(converted_batch)
        stats["raw_capture_chunks"] = copied_audio
        await _insert_many(
            destination["audio_capture_sessions"], capture_plan.capture_sessions
        )

    logger.info(
        "Transforming %d audio evidence spans",
        source_counts_before.get("audio_evidence_spans", 0),
    )
    cursor = source["audio_evidence_spans"].find({}).sort("_id", 1)
    async for batch in _batched(cursor, INSERT_BATCH_SIZE):
        transformed = [
            transform_audio_evidence_span(
                item,
                range_documents,
                materialized_ids,
                allowed_fields=set(AudioEvidenceSpan.model_fields),
            )
            for item in batch
        ]
        if args.apply:
            await destination["audio_evidence_spans"].insert_many(
                transformed, ordered=True
            )
        stats["audio_evidence_spans"] += len(transformed)

    logger.info(
        "Transforming %d device input items (including screenshots)",
        source_counts_before.get("device_input_items", 0),
    )
    cursor = source["device_input_items"].find({}).sort("_id", 1)
    async for batch in _batched(cursor, INSERT_BATCH_SIZE):
        transformed = []
        for item in batch:
            recovered = _recover_promoted_media(item, args.data_dir)
            if recovered is not None:
                data, filename, content_type = recovered
                stats["external_media_embedded"] += 1
                stats["external_media_embedded_bytes"] += len(data)
            else:
                data = filename = content_type = None
            transformed.append(
                transform_device_input_item(
                    item,
                    materialized_ids,
                    allowed_fields=set(DeviceInputItem.model_fields),
                    recovered_media=data,
                    recovered_media_filename=filename,
                    recovered_media_content_type=content_type,
                )
            )
        if args.apply:
            await destination["device_input_items"].insert_many(
                transformed, ordered=True
            )
        stats["device_input_items"] += len(transformed)

    sticky_suppression_count = await source["background_suppressions"].count_documents(
        {"status": {"$in": ["restored", "confirmed"]}}
    )
    stats["sticky_background_suppressions"] = sticky_suppression_count
    if args.apply and sticky_suppression_count:
        cursor = (
            source["background_suppressions"]
            .find({"status": {"$in": ["restored", "confirmed"]}})
            .sort("_id", 1)
        )
        async for batch in _batched(cursor, INSERT_BATCH_SIZE):
            await destination["background_suppressions"].insert_many(
                batch, ordered=True
            )

    if args.apply:
        for name in sorted(COPIED_COLLECTIONS & set(source_names)):
            logger.info("Copying durable collection %s", name)
            stats[f"copied_{name}"] = await _copy_collection(
                source, destination, name, canonical_by_source
            )

    source_counts_after = await _collection_counts(source)
    report["source_counts_after"] = source_counts_after
    source_count_changes = {
        name: {
            "before": source_counts_before.get(name, 0),
            "after": source_counts_after.get(name, 0),
        }
        for name in sorted(set(source_counts_before) | set(source_counts_after))
        if source_counts_before.get(name, 0) != source_counts_after.get(name, 0)
    }
    tolerated_ttl_changes = {}
    system_event_change = source_count_changes.get("system_events")
    if (
        system_event_change
        and system_event_change["after"] < system_event_change["before"]
    ):
        # Mongo's 30-day TTL monitor remains active even with every Chronicle writer
        # stopped. Expiry is the collection's intended retention policy, not corpus
        # drift; the exact rows copied into staging are still counted and validated.
        tolerated_ttl_changes["system_events"] = source_count_changes.pop(
            "system_events"
        )
    report["source_count_changes"] = source_count_changes
    report["tolerated_ttl_count_changes"] = tolerated_ttl_changes
    report["source_changed_during_run"] = bool(source_count_changes)
    report["stats"] = dict(stats)
    report["source_audio_fingerprint"] = (
        _fingerprint_report(source_audio_state) if args.apply else None
    )

    if args.apply:
        copied_expected = {
            name: stats[f"copied_{name}"]
            for name in COPIED_COLLECTIONS
            if name in source_counts_before
        }
        copied_expected["audio_evidence_spans"] = stats["audio_evidence_spans"]
        copied_expected["device_input_items"] = stats["device_input_items"]
        copied_expected["audio_chunks"] = stats["raw_capture_chunks"]
        copied_expected["audio_capture_sessions"] = len(capture_plan.capture_sessions)
        copied_expected["transcript_artifacts"] = stats["transcript_artifacts"]
        copied_expected["diarization_artifacts"] = stats["diarization_artifacts"]
        copied_expected["conversation_transcript_revisions"] = stats[
            "conversation_revisions"
        ]
        copied_expected["background_suppressions"] = sticky_suppression_count
        copied_expected["capture_cutover_quarantine"] = (
            stats["quarantined_audio_chunks"] + stats["quarantined_transcript_records"]
        )
        validation = await _validate_destination(
            destination,
            expected_audio=report["source_audio_fingerprint"],
            expected_counts=copied_expected,
            expected_disabled_audio=sum(
                1 for chunk in chunk_metadata if chunk.get("deleted")
            ),
            materialized_ids=materialized_ids,
        )
        report["validation_before_indexes"] = validation
        if not validation["ok"]:
            raise RuntimeError(
                "Destination validation failed; staging database was not promoted"
            )
        logger.info("Creating current Beanie indexes in the staging database")
        await _initialize_destination_models(destination)
        report["indexes_created"] = True
        logger.info("Validating every transformed document against current models")
        report["model_validated_documents"] = await _validate_transformed_models(
            destination
        )
        if report["source_changed_during_run"]:
            raise RuntimeError(
                "Source changed during cutover; keep this staging database for diagnosis only and rerun with writers stopped"
            )

    report["completed_at"] = datetime.now(timezone.utc)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-database", default=MONGODB_DATABASE)
    parser.add_argument("--destination-database", required=True)
    parser.add_argument(
        "--apply", action="store_true", help="write the empty staging DB"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("DATA_DIR", "data")),
        help="Chronicle data root used to recover path-only media evidence",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "--reconciliation-manifest",
        type=Path,
        required=True,
        help="reviewed identity manifest; blockers prevent staging",
    )
    args = parser.parse_args()
    if args.destination_database == args.source_database:
        parser.error("destination must differ from source")
    if "staging" not in args.destination_database.lower():
        parser.error("destination database name must contain 'staging'")
    return args


async def main() -> None:
    args = parse_args()
    report_path = args.report or _report_path(args.destination_database)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        report = await run(args)
    except Exception as error:
        failure = {
            "source_database": args.source_database,
            "destination_database": args.destination_database,
            "mode": "apply" if args.apply else "dry-run",
            "failed_at": datetime.now(timezone.utc),
            "error": f"{type(error).__name__}: {error}",
        }
        report_path.write_text(
            json.dumps(failure, indent=2, default=_json_default), encoding="utf-8"
        )
        logger.exception("Cutover failed; report: %s", report_path)
        raise
    report["elapsed_seconds"] = time.monotonic() - started
    report_path.write_text(
        json.dumps(report, indent=2, default=_json_default), encoding="utf-8"
    )
    logger.info(
        "Cutover %s complete in %.1fs", report["mode"], report["elapsed_seconds"]
    )
    logger.info("Report: %s", report_path)
    print(json.dumps(report, indent=2, default=_json_default))


if __name__ == "__main__":
    asyncio.run(main())
