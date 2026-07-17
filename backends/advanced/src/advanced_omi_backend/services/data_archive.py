"""Portable, checksummed archives for Chronicle's durable user data.

MongoDB collections are stored as concatenated BSON documents so types and
compressed audio bytes round-trip without JSON coercion. Filesystem-backed
vault and legacy audio files are included as ordinary ZIP members.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator, Optional
from urllib.parse import quote

from bson import BSON
from pymongo import ReplaceOne

from advanced_omi_backend.utils.audio_chunk_utils import decode_opus_to_pcm

ARCHIVE_FORMAT = "chronicle-data-archive"
ARCHIVE_VERSION = 1
ARCHIVE_SUFFIX = ".chronicle"
DATABASE_PREFIX = "database/"
FILES_PREFIX = "files/"
MANIFEST_PATH = "manifest.json"
FILE_ROOTS = ("conversation_docs", "memory_md", "audio_chunks")
DERIVED_MEMORY_COLLECTIONS = frozenset({"memory_audit"})
SYNC_MARKERS = frozenset({".stfolder", ".stignore"})

logger = logging.getLogger(__name__)


class ArchiveError(RuntimeError):
    """Raised when an archive is invalid or cannot be restored safely."""


@dataclass(frozen=True)
class ArchiveSummary:
    path: Path
    collections: int
    documents: int
    files: int
    bytes_written: int


@dataclass(frozen=True)
class ImportSummary:
    collections: int
    documents: int
    files: int
    skipped_collections: tuple[str, ...]
    duplicate_audio_warnings: tuple["DuplicateAudioWarning", ...]
    duplicate_chunk_warnings: tuple["DuplicateChunkWarning", ...] = ()


@dataclass(frozen=True)
class DuplicateAudioWarning:
    kept_conversation_id: str
    skipped_conversation_id: str
    fingerprint: str
    kept_source: str


@dataclass(frozen=True)
class DuplicateChunkWarning:
    conversation_id: str
    chunk_index: int
    kept_chunk_id: str
    skipped_chunk_id: str
    kept_source: str


class _DigestWriter:
    """Track the digest and size of bytes written to another binary stream."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        written = self.stream.write(data)
        if written != len(data):
            raise OSError(f"Short archive write: expected {len(data)}, wrote {written}")
        self.digest.update(data)
        self.size += written
        return written

    @property
    def sha256(self) -> str:
        return self.digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_member_path(member: str) -> PurePosixPath:
    path = PurePosixPath(member)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ArchiveError(f"Unsafe archive member path: {member!r}")
    return path


def _collection_member(collection_name: str) -> str:
    return f"{DATABASE_PREFIX}{quote(collection_name, safe='')}.bson"


def _iter_regular_files(data_dir: Path) -> Iterator[tuple[Path, str]]:
    for root_name in FILE_ROOTS:
        root = data_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(data_dir).as_posix()
            yield path, f"{FILES_PREFIX}{relative}"


async def create_data_archive(
    database: Any,
    output_path: Path,
    *,
    data_dir: Path,
    overwrite: bool = False,
) -> ArchiveSummary:
    """Export all Mongo collections and durable filesystem data to one archive."""
    output_path = output_path.expanduser().resolve()
    if output_path.suffix != ARCHIVE_SUFFIX:
        output_path = output_path.with_name(output_path.name + ARCHIVE_SUFFIX)
    if output_path.exists() and not overwrite:
        raise ArchiveError(f"Archive already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.partial-{os.getpid()}")
    if temp_path.exists():
        temp_path.unlink()

    manifest: dict[str, Any] = {
        "format": ARCHIVE_FORMAT,
        "schema_version": ARCHIVE_VERSION,
        "created_at": _utc_now(),
        "database": getattr(database, "name", None),
        "file_roots": list(FILE_ROOTS),
        "collections": {},
        "files": {},
    }
    total_documents = 0
    total_files = 0

    try:
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            collection_names = sorted(
                name
                for name in await database.list_collection_names()
                if not name.startswith("system.")
            )
            for collection_name in collection_names:
                member = _collection_member(collection_name)
                count = 0
                with archive.open(member, mode="w", force_zip64=True) as stream:
                    writer = _DigestWriter(stream)
                    cursor = database[collection_name].find({})
                    if collection_name == "audio_chunks":
                        cursor = cursor.sort(
                            [
                                ("conversation_id", 1),
                                ("chunk_index", 1),
                                ("created_at", 1),
                                ("_id", 1),
                            ]
                        )
                    async for document in cursor:
                        writer.write(BSON.encode(document))
                        count += 1
                manifest["collections"][collection_name] = {
                    "member": member,
                    "documents": count,
                }
                manifest["files"][member] = {
                    "sha256": writer.sha256,
                    "size": writer.size,
                    "kind": "collection",
                }
                total_documents += count

            for source_path, member in _iter_regular_files(data_dir):
                _safe_member_path(member)
                with source_path.open("rb") as source, archive.open(
                    member, mode="w", force_zip64=True
                ) as stream:
                    writer = _DigestWriter(stream)
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        writer.write(chunk)
                manifest["files"][member] = {
                    "sha256": writer.sha256,
                    "size": writer.size,
                    "kind": "data_file",
                }
                total_files += 1

            archive.writestr(
                MANIFEST_PATH,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )

        temp_path.replace(output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return ArchiveSummary(
        path=output_path,
        collections=len(manifest["collections"]),
        documents=total_documents,
        files=total_files,
        bytes_written=output_path.stat().st_size,
    )


def _load_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ArchiveError("Archive contains duplicate member names")
    try:
        manifest = json.loads(archive.read(MANIFEST_PATH))
    except KeyError as exc:
        raise ArchiveError("Archive has no manifest.json") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("Archive manifest is not valid UTF-8 JSON") from exc

    if manifest.get("format") != ARCHIVE_FORMAT:
        raise ArchiveError(f"Unsupported archive format: {manifest.get('format')!r}")
    if manifest.get("schema_version") != ARCHIVE_VERSION:
        raise ArchiveError(
            f"Unsupported archive schema version: {manifest.get('schema_version')!r}"
        )
    if not isinstance(manifest.get("collections"), dict) or not isinstance(
        manifest.get("files"), dict
    ):
        raise ArchiveError("Archive manifest is missing collections or files")

    expected = {MANIFEST_PATH, *manifest["files"].keys()}
    actual = set(names)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ArchiveError(f"Archive member mismatch; missing={missing}, extra={extra}")
    for member in manifest["files"]:
        _safe_member_path(member)
    return manifest


def verify_data_archive(archive_path: Path) -> dict[str, Any]:
    """Validate structure, CRCs, sizes, and SHA-256 hashes before import."""
    try:
        with zipfile.ZipFile(archive_path, mode="r", allowZip64=True) as archive:
            manifest = _load_manifest(archive)
            for member, expected in manifest["files"].items():
                digest = hashlib.sha256()
                size = 0
                with archive.open(member, mode="r") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                if size != expected.get("size"):
                    raise ArchiveError(
                        f"Archive size mismatch for {member}: expected "
                        f"{expected.get('size')}, got {size}"
                    )
                if digest.hexdigest() != expected.get("sha256"):
                    raise ArchiveError(f"Archive checksum mismatch for {member}")
            return manifest
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Not a valid Chronicle archive: {archive_path}") from exc


def _iter_bson(stream: BinaryIO) -> Iterator[dict[str, Any]]:
    while True:
        length_bytes = stream.read(4)
        if not length_bytes:
            return
        if len(length_bytes) != 4:
            raise ArchiveError("Truncated BSON document length")
        length = int.from_bytes(length_bytes, byteorder="little", signed=True)
        if length < 5 or length > 256 * 1024 * 1024:
            raise ArchiveError(f"Invalid BSON document length: {length}")
        remainder = stream.read(length - 4)
        if len(remainder) != length - 4:
            raise ArchiveError("Truncated BSON document")
        yield BSON(length_bytes + remainder).decode()


def _iter_unique_audio_chunks(
    documents: Iterable[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], Optional[DuplicateChunkWarning]]]:
    """Yield the first chunk for each conversation/index and report later copies."""
    kept: dict[tuple[str, int], str] = {}
    for document in documents:
        conversation_id = str(document.get("conversation_id", ""))
        chunk_index = int(document.get("chunk_index", 0))
        key = (conversation_id, chunk_index)
        chunk_id = str(document.get("_id", ""))
        if key in kept:
            yield document, DuplicateChunkWarning(
                conversation_id=conversation_id,
                chunk_index=chunk_index,
                kept_chunk_id=kept[key],
                skipped_chunk_id=chunk_id,
                kept_source="archive",
            )
            continue
        kept[key] = chunk_id
        yield document, None


def _chunk_digest(document: dict[str, Any]) -> bytes:
    audio_data = document.get("audio_data")
    if not isinstance(audio_data, (bytes, bytearray)):
        raise ArchiveError("Audio chunk has no binary audio_data")
    digest = hashlib.sha256()
    digest.update(int(document.get("chunk_index", 0)).to_bytes(8, "big", signed=False))
    digest.update(int(document.get("sample_rate", 16000)).to_bytes(4, "big"))
    digest.update(int(document.get("channels", 1)).to_bytes(2, "big"))
    digest.update(len(audio_data).to_bytes(8, "big"))
    digest.update(audio_data)
    return digest.digest()


def _finalize_audio_fingerprints(
    chunks: dict[str, list[tuple[int, bytes]]],
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for conversation_id, chunk_digests in chunks.items():
        digest = hashlib.sha256()
        digest.update(len(chunk_digests).to_bytes(8, "big"))
        for chunk_index, chunk_digest in sorted(chunk_digests):
            digest.update(chunk_index.to_bytes(8, "big", signed=False))
            digest.update(chunk_digest)
        fingerprints[conversation_id] = digest.hexdigest()
    return fingerprints


def _finalize_audio_structures(
    chunks: dict[str, list[tuple[int, int, int, int]]],
) -> dict[str, str]:
    structures: dict[str, str] = {}
    for conversation_id, chunk_metadata in chunks.items():
        digest = hashlib.sha256()
        digest.update(len(chunk_metadata).to_bytes(8, "big"))
        for chunk_index, original_size, sample_rate, channels in sorted(chunk_metadata):
            digest.update(chunk_index.to_bytes(8, "big", signed=False))
            digest.update(original_size.to_bytes(8, "big", signed=False))
            digest.update(sample_rate.to_bytes(4, "big", signed=False))
            digest.update(channels.to_bytes(2, "big", signed=False))
        structures[conversation_id] = digest.hexdigest()
    return structures


def _archive_audio_fingerprints(
    archive: zipfile.ZipFile, manifest: dict[str, Any]
) -> dict[str, str]:
    metadata = manifest["collections"].get("audio_chunks")
    if not metadata:
        return {}
    chunks: dict[str, list[tuple[int, bytes]]] = {}
    with archive.open(metadata["member"], mode="r") as stream:
        for document, duplicate in _iter_unique_audio_chunks(_iter_bson(stream)):
            if duplicate:
                continue
            conversation_id = document.get("conversation_id")
            if not conversation_id:
                raise ArchiveError("Audio chunk has no conversation_id")
            chunk_index = int(document.get("chunk_index", 0))
            chunks.setdefault(str(conversation_id), []).append(
                (chunk_index, _chunk_digest(document))
            )
    return _finalize_audio_fingerprints(chunks)


def _archive_audio_structures(
    archive: zipfile.ZipFile, manifest: dict[str, Any]
) -> dict[str, str]:
    metadata = manifest["collections"].get("audio_chunks")
    if not metadata:
        return {}
    chunks: dict[str, list[tuple[int, int, int, int]]] = {}
    with archive.open(metadata["member"], mode="r") as stream:
        for document, duplicate in _iter_unique_audio_chunks(_iter_bson(stream)):
            if duplicate:
                continue
            conversation_id = document.get("conversation_id")
            if not conversation_id:
                raise ArchiveError("Audio chunk has no conversation_id")
            chunks.setdefault(str(conversation_id), []).append(
                (
                    int(document.get("chunk_index", 0)),
                    int(document.get("original_size", 0)),
                    int(document.get("sample_rate", 16000)),
                    int(document.get("channels", 1)),
                )
            )
    return _finalize_audio_structures(chunks)


async def _database_audio_fingerprints(database: Any) -> dict[str, str]:
    chunks: dict[str, list[tuple[int, bytes]]] = {}
    cursor = database["audio_chunks"].find(
        {},
        projection={
            "conversation_id": 1,
            "chunk_index": 1,
            "sample_rate": 1,
            "channels": 1,
            "audio_data": 1,
        },
    )
    async for document in cursor:
        conversation_id = document.get("conversation_id")
        if not conversation_id:
            continue
        chunk_index = int(document.get("chunk_index", 0))
        chunks.setdefault(str(conversation_id), []).append(
            (chunk_index, _chunk_digest(document))
        )
    return _finalize_audio_fingerprints(chunks)


async def _database_audio_structures(database: Any) -> dict[str, str]:
    chunks: dict[str, list[tuple[int, int, int, int]]] = {}
    cursor = database["audio_chunks"].find(
        {},
        projection={
            "conversation_id": 1,
            "chunk_index": 1,
            "original_size": 1,
            "sample_rate": 1,
            "channels": 1,
        },
    )
    async for document in cursor:
        conversation_id = document.get("conversation_id")
        if not conversation_id:
            continue
        chunks.setdefault(str(conversation_id), []).append(
            (
                int(document.get("chunk_index", 0)),
                int(document.get("original_size", 0)),
                int(document.get("sample_rate", 16000)),
                int(document.get("channels", 1)),
            )
        )
    return _finalize_audio_structures(chunks)


async def _pcm_fingerprints(
    chunks: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    semaphore = asyncio.Semaphore(4)

    async def fingerprint_conversation(
        conversation_id: str, documents: list[dict[str, Any]]
    ) -> tuple[str, str]:
        async with semaphore:
            digest = hashlib.sha256()
            for document in sorted(
                documents, key=lambda item: item.get("chunk_index", 0)
            ):
                try:
                    pcm = await decode_opus_to_pcm(
                        bytes(document["audio_data"]),
                        int(document.get("sample_rate", 16000)),
                        int(document.get("channels", 1)),
                    )
                except Exception as exc:
                    raise ArchiveError(
                        f"Could not decode audio for duplicate check: {conversation_id}"
                    ) from exc
                digest.update(pcm)
            return conversation_id, digest.hexdigest()

    results = await asyncio.gather(
        *(
            fingerprint_conversation(conversation_id, documents)
            for conversation_id, documents in chunks.items()
        )
    )
    return dict(results)


async def _archive_pcm_fingerprints(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    conversation_ids: set[str],
) -> dict[str, str]:
    if not conversation_ids:
        return {}
    metadata = manifest["collections"].get("audio_chunks")
    if not metadata:
        return {}
    chunks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with archive.open(metadata["member"], mode="r") as stream:
        for document, duplicate in _iter_unique_audio_chunks(_iter_bson(stream)):
            if duplicate:
                continue
            conversation_id = str(document.get("conversation_id", ""))
            if conversation_id in conversation_ids:
                chunks[conversation_id].append(document)
    return await _pcm_fingerprints(chunks)


async def _database_pcm_fingerprints(
    database: Any, conversation_ids: set[str]
) -> dict[str, str]:
    if not conversation_ids:
        return {}
    chunks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cursor = database["audio_chunks"].find(
        {"conversation_id": {"$in": sorted(conversation_ids)}},
        projection={
            "conversation_id": 1,
            "chunk_index": 1,
            "sample_rate": 1,
            "channels": 1,
            "audio_data": 1,
        },
    )
    async for document in cursor:
        conversation_id = str(document.get("conversation_id", ""))
        if conversation_id in conversation_ids:
            chunks[conversation_id].append(document)
    return await _pcm_fingerprints(chunks)


def _conversation_sort_key(document: dict[str, Any]) -> tuple[float, str]:
    created_at = document.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        timestamp = created_at.timestamp()
    else:
        timestamp = float("inf")
    return timestamp, str(document.get("conversation_id", ""))


def _archive_conversation_sort_keys(
    archive: zipfile.ZipFile, manifest: dict[str, Any]
) -> dict[str, tuple[float, str]]:
    metadata = manifest["collections"].get("conversations")
    if not metadata:
        return {}
    sort_keys: dict[str, tuple[float, str]] = {}
    with archive.open(metadata["member"], mode="r") as stream:
        for document in _iter_bson(stream):
            conversation_id = document.get("conversation_id")
            if conversation_id:
                sort_keys[str(conversation_id)] = _conversation_sort_key(document)
    return sort_keys


async def _database_conversation_sort_keys(
    database: Any, conversation_ids: set[str]
) -> dict[str, tuple[float, str]]:
    if not conversation_ids:
        return {}
    keys: dict[str, tuple[float, str]] = {}
    cursor = database["conversations"].find(
        {"conversation_id": {"$in": sorted(conversation_ids)}},
        projection={"conversation_id": 1, "created_at": 1},
    )
    async for document in cursor:
        conversation_id = document.get("conversation_id")
        if conversation_id:
            keys[str(conversation_id)] = _conversation_sort_key(document)
    return keys


async def _duplicate_audio_plan(
    database: Any,
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    *,
    replace: bool,
) -> tuple[set[str], tuple[DuplicateAudioWarning, ...]]:
    archive_fingerprints = _archive_audio_fingerprints(archive, manifest)
    archive_structures = _archive_audio_structures(archive, manifest)
    existing_fingerprints: dict[str, str] = {}
    existing_structures: dict[str, str] = {}
    if not replace:
        existing_fingerprints = await _database_audio_fingerprints(database)
        existing_structures = await _database_audio_structures(database)

    conversations_by_structure: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for conversation_id, structure in archive_structures.items():
        conversations_by_structure[structure].append(("archive", conversation_id))
    for conversation_id, structure in existing_structures.items():
        conversations_by_structure[structure].append(("existing", conversation_id))
    candidate_archive_ids: set[str] = set()
    candidate_existing_ids: set[str] = set()
    for candidates in conversations_by_structure.values():
        if len(candidates) < 2 or not any(
            source == "archive" for source, _ in candidates
        ):
            continue
        candidate_archive_ids.update(
            conversation_id
            for source, conversation_id in candidates
            if source == "archive"
        )
        candidate_existing_ids.update(
            conversation_id
            for source, conversation_id in candidates
            if source == "existing"
        )

    archive_pcm = await _archive_pcm_fingerprints(
        archive, manifest, candidate_archive_ids
    )
    existing_pcm = await _database_pcm_fingerprints(database, candidate_existing_ids)
    for conversation_id, fingerprint in archive_pcm.items():
        archive_fingerprints[conversation_id] = f"pcm:{fingerprint}"
    for conversation_id, fingerprint in existing_pcm.items():
        existing_fingerprints[conversation_id] = f"pcm:{fingerprint}"
    for conversation_id in archive_fingerprints.keys() - archive_pcm.keys():
        archive_fingerprints[conversation_id] = (
            f"compressed:{archive_fingerprints[conversation_id]}"
        )
    for conversation_id in existing_fingerprints.keys() - existing_pcm.keys():
        existing_fingerprints[conversation_id] = (
            f"compressed:{existing_fingerprints[conversation_id]}"
        )

    archive_sort_keys = _archive_conversation_sort_keys(archive, manifest)
    by_fingerprint: dict[str, list[str]] = {}
    for conversation_id, fingerprint in archive_fingerprints.items():
        by_fingerprint.setdefault(fingerprint, []).append(conversation_id)

    skipped: set[str] = set()
    warnings: list[DuplicateAudioWarning] = []
    archive_winners: dict[str, str] = {}
    for fingerprint, conversation_ids in by_fingerprint.items():
        ordered = sorted(
            conversation_ids,
            key=lambda item: archive_sort_keys.get(item, (float("inf"), item)),
        )
        winner = ordered[0]
        archive_winners[fingerprint] = winner
        for duplicate in ordered[1:]:
            skipped.add(duplicate)
            warnings.append(
                DuplicateAudioWarning(
                    kept_conversation_id=winner,
                    skipped_conversation_id=duplicate,
                    fingerprint=fingerprint,
                    kept_source="archive",
                )
            )

    if not replace:
        existing_by_fingerprint: dict[str, set[str]] = {}
        for conversation_id, fingerprint in existing_fingerprints.items():
            existing_by_fingerprint.setdefault(fingerprint, set()).add(conversation_id)
        existing_ids = {
            conversation_id
            for ids in existing_by_fingerprint.values()
            for conversation_id in ids
        }
        existing_sort_keys = await _database_conversation_sort_keys(
            database, existing_ids
        )
        for fingerprint, archive_winner in archive_winners.items():
            existing_ids_for_audio = existing_by_fingerprint.get(fingerprint, set())
            if not existing_ids_for_audio or archive_winner in existing_ids_for_audio:
                continue
            existing_winner = min(
                existing_ids_for_audio,
                key=lambda item: existing_sort_keys.get(item, (float("inf"), item)),
            )
            skipped.add(archive_winner)
            warnings.append(
                DuplicateAudioWarning(
                    kept_conversation_id=existing_winner,
                    skipped_conversation_id=archive_winner,
                    fingerprint=fingerprint,
                    kept_source="existing_database",
                )
            )

    for warning in warnings:
        logger.warning(
            "Duplicate audio skipped during import: conversation %s matches %s %s "
            "(fingerprint=%s)",
            warning.skipped_conversation_id,
            warning.kept_source,
            warning.kept_conversation_id,
            warning.fingerprint,
        )
    return skipped, tuple(warnings)


async def _restore_collection(
    database: Any,
    archive: zipfile.ZipFile,
    collection_name: str,
    member: str,
    expected_count: int,
    *,
    replace: bool,
    skipped_conversation_ids: set[str],
    batch_size: int = 500,
) -> tuple[int, tuple[DuplicateChunkWarning, ...]]:
    collection = database[collection_name]
    if replace:
        await collection.delete_many({})

    restored = 0
    scanned = 0
    chunk_warnings: list[DuplicateChunkWarning] = []
    archive_chunk_keys: dict[tuple[str, int], str] = {}
    existing_chunk_keys: dict[tuple[str, int], str] = {}
    if collection_name == "audio_chunks" and not replace:
        cursor = collection.find(
            {}, projection={"conversation_id": 1, "chunk_index": 1}
        )
        async for existing in cursor:
            key = (
                str(existing.get("conversation_id", "")),
                int(existing.get("chunk_index", 0)),
            )
            existing_chunk_keys.setdefault(key, str(existing.get("_id", "")))
    operations: list[ReplaceOne] = []
    with archive.open(member, mode="r") as stream:
        for document in _iter_bson(stream):
            scanned += 1
            if "_id" not in document:
                raise ArchiveError(f"Document in {collection_name} has no _id")
            if str(document.get("conversation_id", "")) in skipped_conversation_ids:
                continue
            if collection_name == "audio_chunks":
                key = (
                    str(document.get("conversation_id", "")),
                    int(document.get("chunk_index", 0)),
                )
                chunk_id = str(document["_id"])
                kept_id = existing_chunk_keys.get(key) or archive_chunk_keys.get(key)
                if kept_id is not None:
                    warning = DuplicateChunkWarning(
                        conversation_id=key[0],
                        chunk_index=key[1],
                        kept_chunk_id=kept_id,
                        skipped_chunk_id=chunk_id,
                        kept_source=(
                            "existing_database"
                            if key in existing_chunk_keys
                            else "archive"
                        ),
                    )
                    chunk_warnings.append(warning)
                    logger.warning(
                        "Duplicate audio chunk skipped during import: conversation %s "
                        "chunk %d (%s kept, skipped _id=%s)",
                        warning.conversation_id,
                        warning.chunk_index,
                        warning.kept_source,
                        warning.skipped_chunk_id,
                    )
                    continue
                archive_chunk_keys[key] = chunk_id
            operations.append(
                ReplaceOne({"_id": document["_id"]}, document, upsert=True)
            )
            if len(operations) >= batch_size:
                await collection.bulk_write(operations, ordered=False)
                restored += len(operations)
                operations.clear()
        if operations:
            await collection.bulk_write(operations, ordered=False)
            restored += len(operations)
    if scanned != expected_count:
        raise ArchiveError(
            f"Document count mismatch for {collection_name}: "
            f"expected {expected_count}, scanned {scanned}"
        )
    return restored, tuple(chunk_warnings)


def _destination_for_data_member(data_dir: Path, member: str) -> Path:
    member_path = _safe_member_path(member)
    prefix = PurePosixPath(FILES_PREFIX.rstrip("/"))
    try:
        relative = member_path.relative_to(prefix)
    except ValueError as exc:
        raise ArchiveError(f"Not a filesystem data member: {member}") from exc
    if not relative.parts or relative.parts[0] not in FILE_ROOTS:
        raise ArchiveError(f"Unsupported filesystem data root: {member}")
    destination = data_dir.joinpath(*relative.parts)
    if data_dir.resolve() not in destination.resolve().parents:
        raise ArchiveError(f"Filesystem member escapes data directory: {member}")
    return destination


def clear_vault_contents(user_root: Path) -> int:
    """Delete derived vault contents while retaining Syncthing pairing markers."""
    if not user_root.is_dir():
        return 0
    deleted = 0
    for entry in user_root.iterdir():
        if entry.name in SYNC_MARKERS:
            continue
        if entry.is_dir() and not entry.is_symlink():
            deleted += sum(1 for path in entry.rglob("*") if path.is_file())
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)
            deleted += 1
    return deleted


def _clear_restored_file_roots(data_dir: Path, root_names: Iterable[str]) -> None:
    for root_name in root_names:
        if root_name not in FILE_ROOTS:
            raise ArchiveError(f"Unsupported filesystem data root: {root_name}")
        root = data_dir / root_name
        if not root.is_dir():
            continue
        if root_name in ("conversation_docs", "memory_md"):
            for entry in root.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    clear_vault_contents(entry)
                elif entry.name not in SYNC_MARKERS:
                    entry.unlink(missing_ok=True)
            continue
        for entry in root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)


def _restore_data_files(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    data_dir: Path,
    *,
    replace: bool,
) -> int:
    members = [
        member
        for member, metadata in manifest["files"].items()
        if metadata.get("kind") == "data_file"
    ]
    if replace:
        _clear_restored_file_roots(
            data_dir, manifest.get("file_roots", list(FILE_ROOTS))
        )
    restored = 0
    for member in members:
        destination = _destination_for_data_member(data_dir, member)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
        try:
            with archive.open(member, mode="r") as source, temp_path.open(
                "wb"
            ) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            temp_path.replace(destination)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        restored += 1
    return restored


async def import_data_archive(
    database: Any,
    archive_path: Path,
    *,
    data_dir: Path,
    replace: bool = False,
    restore_files: bool = True,
    fresh_memory: bool = False,
) -> ImportSummary:
    """Verify and import an archive, optionally excluding all derived memory state."""
    manifest = verify_data_archive(archive_path)
    if fresh_memory and restore_files:
        raise ArchiveError("fresh_memory cannot be combined with restore_files")

    skipped = set(DERIVED_MEMORY_COLLECTIONS if fresh_memory else ())
    restored_collections = 0
    restored_documents = 0
    restored_files = 0
    duplicate_warnings: tuple[DuplicateAudioWarning, ...] = ()
    duplicate_chunk_warnings: list[DuplicateChunkWarning] = []
    with zipfile.ZipFile(archive_path, mode="r", allowZip64=True) as archive:
        skipped_conversation_ids, duplicate_warnings = await _duplicate_audio_plan(
            database, archive, manifest, replace=replace
        )
        for collection_name, metadata in manifest["collections"].items():
            if collection_name in skipped:
                continue
            restored_count, collection_chunk_warnings = await _restore_collection(
                database,
                archive,
                collection_name,
                metadata["member"],
                metadata["documents"],
                replace=replace,
                skipped_conversation_ids=skipped_conversation_ids,
            )
            restored_documents += restored_count
            duplicate_chunk_warnings.extend(collection_chunk_warnings)
            restored_collections += 1
        if restore_files:
            restored_files = _restore_data_files(
                archive, manifest, data_dir, replace=replace
            )

    return ImportSummary(
        collections=restored_collections,
        documents=restored_documents,
        files=restored_files,
        skipped_collections=tuple(sorted(skipped)),
        duplicate_audio_warnings=duplicate_warnings,
        duplicate_chunk_warnings=tuple(duplicate_chunk_warnings),
    )
