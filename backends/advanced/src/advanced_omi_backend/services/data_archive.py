"""Portable, checksummed archives for Chronicle's durable user data.

MongoDB collections are stored as concatenated BSON documents so types and
compressed audio bytes round-trip without JSON coercion. Filesystem-backed
vault and legacy audio files are included as ordinary ZIP members.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Optional
from urllib.parse import quote

from bson import BSON
from pymongo import ReplaceOne

ARCHIVE_FORMAT = "chronicle-data-archive"
ARCHIVE_VERSION = 2
ARCHIVE_SUFFIX = ".chronicle"
DATABASE_PREFIX = "database/"
FILES_PREFIX = "files/"
MANIFEST_PATH = "manifest.json"
FILE_ROOTS = (
    "conversation_docs",
    "memory_md",
    "audio_chunks",
    "inference_artifacts",
    "pi_operating_memory",
)
DERIVED_MEMORY_COLLECTIONS = frozenset({"memory_audit"})
SYNC_MARKERS = frozenset({".stfolder", ".stignore"})


class ArchiveError(RuntimeError):
    """Raised when an archive is invalid or cannot be restored safely."""


@dataclass(frozen=True)
class ArchiveSummary:
    path: Path
    collections: int
    documents: int
    files: int
    bytes_written: int
    excluded_audio_chunks: int = 0
    excluded_documents: int = 0
    excluded_files: int = 0


@dataclass(frozen=True)
class ImportSummary:
    collections: int
    documents: int
    files: int
    skipped_collections: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveProgress:
    """One observable checkpoint in an archive operation."""

    stage: str
    current: int
    total: int
    unit: str
    detail: str
    completed: bool = False


ProgressCallback = Callable[[ArchiveProgress], None]


@dataclass(frozen=True)
class VerifiedArchive:
    """A checksum-verified archive snapshot that has not changed on disk."""

    path: Path
    size: int
    mtime_ns: int
    manifest: dict[str, Any]


@dataclass(frozen=True)
class BaseArchiveState:
    """Cumulative snapshot indexes carried by one or more verified bases."""

    audio_chunk_ids: frozenset[str]
    document_digests: dict[str, dict[str, str]]
    data_files: dict[str, dict[str, Any]]
    references: list[dict[str, Any]]


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


def _report_progress(
    callback: Optional[ProgressCallback],
    *,
    stage: str,
    current: int,
    total: int,
    unit: str,
    detail: str,
    completed: bool = False,
) -> None:
    if callback is None:
        return
    callback(
        ArchiveProgress(
            stage=stage,
            current=current,
            total=total,
            unit=unit,
            detail=detail,
            completed=completed,
        )
    )


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


def _encoded_document(document: dict[str, Any]) -> tuple[bytes, str]:
    encoded = BSON.encode(document)
    return encoded, hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def create_data_archive(
    database: Any,
    output_path: Path,
    *,
    data_dir: Path,
    overwrite: bool = False,
    exclude_audio_chunk_ids: Iterable[str] = (),
    base_archives: Iterable[Path] = (),
    progress: Optional[ProgressCallback] = None,
) -> ArchiveSummary:
    """Export a restorable snapshot, storing only changes relative to verified bases.

    Mongo documents (including capture chunks) and filesystem files are deduplicated
    by content digest, with deletion tombstones in the manifest. An unchanged audio
    chunk therefore contributes no audio bytes to an incremental archive, while a
    metadata change on the same chunk ID is still restorable. Restoring an incremental
    archive requires its verified base chain to have been restored first.
    """
    base_state = collect_base_archive_state(base_archives)
    explicitly_excluded_audio = frozenset(
        str(value) for value in exclude_audio_chunk_ids
    )
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
        "excluded_audio_chunk_ids": [],
        "document_digests": {},
        "excluded_document_ids": {},
        "deleted_document_ids": {},
        "data_file_index": {},
        "excluded_data_files": [],
        "deleted_data_files": [],
        "base_archives": base_state.references,
    }
    total_documents = 0
    total_files = 0
    excluded_chunks = 0
    excluded_documents = 0
    excluded_files = 0

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
            collection_counts = {
                name: await database[name].count_documents({})
                for name in collection_names
            }
            export_document_total = sum(collection_counts.values())
            scanned_documents = 0
            _report_progress(
                progress,
                stage="export_database",
                current=0,
                total=export_document_total,
                unit="documents",
                detail=f"Exporting {len(collection_names):,} MongoDB collections",
            )
            for collection_name in collection_names:
                member = _collection_member(collection_name)
                count = 0
                current_document_digests: dict[str, str] = {}
                omitted_document_ids: list[str] = []
                omitted_audio_ids: list[str] = []
                base_document_digests = base_state.document_digests.get(
                    collection_name, {}
                )
                with archive.open(member, mode="w", force_zip64=True) as stream:
                    writer = _DigestWriter(stream)
                    cursor = database[collection_name].find({})
                    if collection_name == "audio_chunks":
                        cursor = cursor.sort(
                            [
                                ("capture_session_id", 1),
                                ("sequence", 1),
                                ("captured_at", 1),
                                ("created_at", 1),
                                ("_id", 1),
                            ]
                        )
                    async for document in cursor:
                        scanned_documents += 1
                        if scanned_documents % 500 == 0:
                            _report_progress(
                                progress,
                                stage="export_database",
                                current=scanned_documents,
                                total=export_document_total,
                                unit="documents",
                                detail=collection_name,
                            )
                        document_id = str(document.get("_id", ""))
                        encoded, digest = _encoded_document(document)
                        current_document_digests[document_id] = digest
                        if collection_name == "audio_chunks":
                            if (
                                document_id in explicitly_excluded_audio
                                or base_document_digests.get(document_id) == digest
                            ):
                                omitted_audio_ids.append(document_id)
                                excluded_chunks += 1
                                continue
                        elif base_document_digests.get(document_id) == digest:
                            omitted_document_ids.append(document_id)
                            excluded_documents += 1
                            continue
                        writer.write(encoded)
                        count += 1
                manifest["document_digests"][collection_name] = current_document_digests
                if omitted_audio_ids:
                    omitted_audio_ids.sort()
                    manifest["excluded_audio_chunk_ids"].extend(omitted_audio_ids)
                    # The digest-bearing generic index makes restore verify that the
                    # base has the exact omitted chunk document, not merely the same ID.
                    manifest["excluded_document_ids"][
                        collection_name
                    ] = omitted_audio_ids
                elif omitted_document_ids:
                    manifest["excluded_document_ids"][collection_name] = sorted(
                        omitted_document_ids
                    )
                deleted_ids = sorted(
                    set(base_document_digests) - set(current_document_digests)
                )
                if deleted_ids:
                    manifest["deleted_document_ids"][collection_name] = deleted_ids
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
            for collection_name, base_digests in base_state.document_digests.items():
                if collection_name in collection_names or not base_digests:
                    continue
                manifest["document_digests"][collection_name] = {}
                manifest["deleted_document_ids"][collection_name] = sorted(base_digests)
            manifest["excluded_audio_chunk_ids"].sort()
            _report_progress(
                progress,
                stage="export_database",
                current=export_document_total,
                total=export_document_total,
                unit="documents",
                detail=f"Exported {total_documents:,} MongoDB documents",
                completed=True,
            )

            regular_files = list(_iter_regular_files(data_dir))
            export_file_bytes = sum(path.stat().st_size for path, _ in regular_files)
            exported_file_bytes = 0
            current_file_index: dict[str, dict[str, Any]] = {}
            _report_progress(
                progress,
                stage="export_files",
                current=0,
                total=export_file_bytes,
                unit="bytes",
                detail=f"Exporting {len(regular_files):,} filesystem files",
            )
            for source_path, member in regular_files:
                _safe_member_path(member)
                sha256, size = _file_digest(source_path)
                exported_file_bytes += size
                metadata = {"sha256": sha256, "size": size}
                current_file_index[member] = metadata
                _report_progress(
                    progress,
                    stage="export_files",
                    current=exported_file_bytes,
                    total=export_file_bytes,
                    unit="bytes",
                    detail=member,
                )
                if base_state.data_files.get(member) == metadata:
                    manifest["excluded_data_files"].append(member)
                    excluded_files += 1
                    continue
                with source_path.open("rb") as source, archive.open(
                    member, mode="w", force_zip64=True
                ) as stream:
                    writer = _DigestWriter(stream)
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        writer.write(chunk)
                if writer.sha256 != sha256 or writer.size != size:
                    raise ArchiveError(
                        f"Data file changed during export: {source_path}"
                    )
                manifest["files"][member] = {
                    **metadata,
                    "kind": "data_file",
                }
                total_files += 1
            manifest["data_file_index"] = current_file_index
            manifest["excluded_data_files"].sort()
            manifest["deleted_data_files"] = sorted(
                set(base_state.data_files) - set(current_file_index)
            )
            _report_progress(
                progress,
                stage="export_files",
                current=export_file_bytes,
                total=export_file_bytes,
                unit="bytes",
                detail=f"Exported {total_files:,} filesystem files",
                completed=True,
            )

            _report_progress(
                progress,
                stage="finalize_archive",
                current=0,
                total=1,
                unit="steps",
                detail="Writing manifest and committing archive",
            )
            archive.writestr(
                MANIFEST_PATH,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )

        temp_path.replace(output_path)
        _report_progress(
            progress,
            stage="finalize_archive",
            current=1,
            total=1,
            unit="steps",
            detail="Archive committed atomically",
            completed=True,
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return ArchiveSummary(
        path=output_path,
        collections=len(manifest["collections"]),
        documents=total_documents,
        files=total_files,
        bytes_written=output_path.stat().st_size,
        excluded_audio_chunks=excluded_chunks,
        excluded_documents=excluded_documents,
        excluded_files=excluded_files,
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


def verify_data_archive(
    archive_path: Path,
    *,
    progress: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """Validate structure, CRCs, sizes, and SHA-256 hashes before import."""
    try:
        with zipfile.ZipFile(archive_path, mode="r", allowZip64=True) as archive:
            manifest = _load_manifest(archive)
            total_bytes = sum(
                int(metadata.get("size", 0)) for metadata in manifest["files"].values()
            )
            verified_bytes = 0
            _report_progress(
                progress,
                stage="verify",
                current=0,
                total=total_bytes,
                unit="bytes",
                detail="Reading archive members",
            )
            for member, expected in manifest["files"].items():
                digest = hashlib.sha256()
                size = 0
                with archive.open(member, mode="r") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        verified_bytes += len(chunk)
                        _report_progress(
                            progress,
                            stage="verify",
                            current=verified_bytes,
                            total=total_bytes,
                            unit="bytes",
                            detail=member,
                        )
                if size != expected.get("size"):
                    raise ArchiveError(
                        f"Archive size mismatch for {member}: expected "
                        f"{expected.get('size')}, got {size}"
                    )
                if digest.hexdigest() != expected.get("sha256"):
                    raise ArchiveError(f"Archive checksum mismatch for {member}")
            _report_progress(
                progress,
                stage="verify",
                current=total_bytes,
                total=total_bytes,
                unit="bytes",
                detail="All archive checksums passed",
                completed=True,
            )
            return manifest
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Not a valid Chronicle archive: {archive_path}") from exc


def verify_data_archive_snapshot(
    archive_path: Path,
    *,
    progress: Optional[ProgressCallback] = None,
) -> VerifiedArchive:
    """Verify an archive and bind the result to its current filesystem identity."""
    resolved = archive_path.expanduser().resolve()
    before = resolved.stat()
    manifest = verify_data_archive(resolved, progress=progress)
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ArchiveError("Archive changed while it was being verified")
    return VerifiedArchive(
        path=resolved,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        manifest=manifest,
    )


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


def collect_base_archive_state(archive_paths: Iterable[Path]) -> BaseArchiveState:
    """Load cumulative IDs and digests from checksum-verified base archives."""
    chunk_ids: set[str] = set()
    document_digests: dict[str, dict[str, str]] = {}
    data_files: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for archive_path in archive_paths:
        resolved = archive_path.expanduser().resolve()
        manifest = verify_data_archive(resolved)
        indexed_documents = manifest.get("document_digests") or {}
        indexed_audio = indexed_documents.get("audio_chunks")
        inherited_ids = {
            str(chunk_id) for chunk_id in manifest.get("excluded_audio_chunk_ids", [])
        }
        physical_ids: set[str] = set()
        physical_audio_digests: dict[str, str] = {}
        with zipfile.ZipFile(resolved, mode="r", allowZip64=True) as archive:
            metadata = manifest["collections"].get("audio_chunks")
            if isinstance(indexed_audio, dict):
                # Current archives carry the cumulative audio-document digest index.
                # IDs absent from the omitted set are the documents physically stored
                # in this delta, so there is no reason to decode the multi-GB BSON
                # stream again after checksum verification.
                physical_ids = {
                    str(document_id) for document_id in indexed_audio
                } - inherited_ids
            elif metadata:
                with archive.open(metadata["member"], mode="r") as stream:
                    for document in _iter_bson(stream):
                        if "_id" not in document:
                            raise ArchiveError(
                                f"Audio chunk in base archive {resolved} has no _id"
                            )
                        document_id = str(document["_id"])
                        physical_ids.add(document_id)
                        encoded, digest = _encoded_document(document)
                        del encoded
                        physical_audio_digests[document_id] = digest

            # Early version-2 archives did not index audio document digests. Derive
            # them while scanning their physical chunks so they remain valid bases
            # for digest-safe incrementals.
            document_digests.setdefault("audio_chunks", {}).update(
                physical_audio_digests
            )

            for collection_name, deleted_ids in (
                manifest.get("deleted_document_ids") or {}
            ).items():
                current = document_digests.setdefault(collection_name, {})
                for document_id in deleted_ids or []:
                    current.pop(str(document_id), None)
            for collection_name, digests in indexed_documents.items():
                current = document_digests.setdefault(collection_name, {})
                current.update(
                    {
                        str(document_id): str(digest)
                        for document_id, digest in (digests or {}).items()
                    }
                )
            # Archives created before document-level incrementality have complete
            # physical collections. Derive their index so they remain valid bases.
            for collection_name, collection_meta in manifest["collections"].items():
                if (
                    collection_name == "audio_chunks"
                    or collection_name in indexed_documents
                ):
                    continue
                derived: dict[str, str] = {}
                with archive.open(collection_meta["member"], mode="r") as stream:
                    for document in _iter_bson(stream):
                        if "_id" not in document:
                            raise ArchiveError(
                                f"Document in base archive {resolved} has no _id"
                            )
                        encoded, digest = _encoded_document(document)
                        del encoded
                        derived[str(document["_id"])] = digest
                document_digests.setdefault(collection_name, {}).update(derived)

        indexed_files = manifest.get("data_file_index") or {}
        for member in manifest.get("deleted_data_files") or []:
            data_files.pop(str(member), None)
        if indexed_files:
            data_files.update(
                {
                    str(member): {
                        "sha256": str(metadata["sha256"]),
                        "size": int(metadata["size"]),
                    }
                    for member, metadata in indexed_files.items()
                }
            )
        else:
            # Older archives stored every durable file physically.
            data_files.update(
                {
                    member: {
                        "sha256": str(metadata["sha256"]),
                        "size": int(metadata["size"]),
                    }
                    for member, metadata in manifest["files"].items()
                    if metadata.get("kind") == "data_file"
                }
            )
        deleted_audio_ids = {
            str(value)
            for value in (manifest.get("deleted_document_ids") or {}).get(
                "audio_chunks", []
            )
        }
        chunk_ids.difference_update(deleted_audio_ids)
        chunk_ids.update(inherited_ids)
        chunk_ids.update(physical_ids)
        manifest_digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        references.append(
            {
                "filename": resolved.name,
                "created_at": manifest.get("created_at"),
                "manifest_sha256": manifest_digest,
                "physical_audio_chunks": len(physical_ids),
                "inherited_audio_chunks": len(inherited_ids),
                "indexed_documents": sum(
                    len(values) for values in document_digests.values()
                ),
                "indexed_data_files": len(data_files),
            }
        )
    return BaseArchiveState(
        audio_chunk_ids=frozenset(chunk_ids),
        document_digests=document_digests,
        data_files=data_files,
        references=references,
    )


def collect_archived_audio_chunk_ids(
    archive_paths: Iterable[Path],
) -> tuple[frozenset[str], list[dict[str, Any]]]:
    """Compatibility-sized view used by the audio-chain audit CLI."""
    state = collect_base_archive_state(archive_paths)
    return state.audio_chunk_ids, state.references


async def _restore_collection(
    database: Any,
    archive: zipfile.ZipFile,
    collection_name: str,
    member: str,
    expected_count: int,
    *,
    replace: bool,
    batch_size: int = 500,
    progress: Optional[ProgressCallback] = None,
    progress_offset: int = 0,
    progress_total: int = 0,
) -> int:
    collection = database[collection_name]
    if replace:
        await collection.delete_many({})

    restored = 0
    scanned = 0
    archive_chunk_keys: dict[tuple[str, int], str] = {}
    existing_chunk_keys: dict[tuple[str, int], str] = {}
    if collection_name == "audio_chunks" and not replace:
        cursor = collection.find(
            {}, projection={"capture_session_id": 1, "sequence": 1}
        )
        async for existing in cursor:
            if not existing.get("capture_session_id") or "sequence" not in existing:
                continue
            key = (
                str(existing["capture_session_id"]),
                int(existing["sequence"]),
            )
            existing_chunk_keys.setdefault(key, str(existing.get("_id", "")))
    operations: list[ReplaceOne] = []
    with archive.open(member, mode="r") as stream:
        for document in _iter_bson(stream):
            scanned += 1
            if scanned % batch_size == 0:
                _report_progress(
                    progress,
                    stage="restore_database",
                    current=progress_offset + scanned,
                    total=progress_total,
                    unit="documents",
                    detail=(
                        f"{collection_name}: {scanned:,}/{expected_count:,} documents"
                    ),
                )
            if "_id" not in document:
                raise ArchiveError(f"Document in {collection_name} has no _id")
            if collection_name == "audio_chunks":
                capture_session_id = document.get("capture_session_id")
                has_capture_id = bool(capture_session_id)
                has_sequence = "sequence" in document
                if has_capture_id != has_sequence:
                    raise ArchiveError(
                        "Archive audio chunk has only part of its capture identity"
                    )
                if has_capture_id:
                    key = (
                        str(capture_session_id),
                        int(document["sequence"]),
                    )
                    chunk_id = str(document["_id"])
                    kept_id = existing_chunk_keys.get(key) or archive_chunk_keys.get(
                        key
                    )
                    if kept_id is not None and kept_id != chunk_id:
                        source = (
                            "existing database"
                            if key in existing_chunk_keys
                            else "archive"
                        )
                        raise ArchiveError(
                            "Conflicting immutable audio chunk identity "
                            f"{key[0]}:{key[1]}: {source} has _id={kept_id}, "
                            f"archive has _id={chunk_id}"
                        )
                    archive_chunk_keys[key] = chunk_id
                else:
                    # A self-contained snapshot taken immediately before the approved
                    # capture-schema cutover must remain an exact rollback artifact.
                    # This is an archive restore seam, not a runtime dual-read model.
                    if not replace:
                        raise ArchiveError(
                            "Pre-cutover audio archives require --replace into an isolated database"
                        )
                    if (
                        "conversation_id" not in document
                        or "chunk_index" not in document
                    ):
                        raise ArchiveError(
                            "Archive audio chunk has neither capture nor pre-cutover identity"
                        )
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
    _report_progress(
        progress,
        stage="restore_database",
        current=progress_offset + scanned,
        total=progress_total,
        unit="documents",
        detail=f"Completed {collection_name} ({scanned:,} documents)",
    )
    return restored


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


async def _collection_documents_by_string_id(
    database: Any, collection_name: str, wanted: set[str]
) -> dict[str, dict[str, Any]]:
    if not wanted:
        return {}
    found: dict[str, dict[str, Any]] = {}
    async for document in database[collection_name].find({}):
        document_id = str(document.get("_id", ""))
        if document_id in wanted:
            found[document_id] = document
            if len(found) == len(wanted):
                break
    return found


async def _verify_collection_documents(
    database: Any,
    collection_name: str,
    required: set[str],
    expected_digests: Optional[dict[str, str]] = None,
) -> None:
    """Stream required documents from Mongo without retaining their payload bytes."""
    if not required:
        return
    remaining = set(required)
    projection = None if expected_digests is not None else {"_id": 1}
    async for document in database[collection_name].find({}, projection=projection):
        document_id = str(document.get("_id", ""))
        if document_id not in remaining:
            continue
        if expected_digests is not None:
            expected_digest = expected_digests.get(document_id)
            if expected_digest is None:
                raise ArchiveError(
                    "Incremental archive has no expected digest for "
                    f"{collection_name}/{document_id}"
                )
            _, actual_digest = _encoded_document(document)
            if actual_digest != expected_digest:
                raise ArchiveError(
                    "Incremental archive base document changed: "
                    f"{collection_name}/{document_id}"
                )
        remaining.remove(document_id)
        if not remaining:
            break
    if remaining:
        missing = sorted(remaining)
        raise ArchiveError(
            "Incremental archive base is incomplete: missing "
            f"{collection_name} document {missing[0]} "
            f"(and {len(missing) - 1} more)"
        )


async def _verify_incremental_base_presence(
    database: Any,
    manifest: dict[str, Any],
    data_dir: Path,
    *,
    restore_files: bool,
) -> None:
    """Fail before mutation when an incremental archive's base is not present."""
    audio_ids = {str(value) for value in manifest.get("excluded_audio_chunk_ids", [])}
    digest_index = manifest.get("document_digests") or {}
    excluded_documents = manifest.get("excluded_document_ids") or {}
    digest_audio_ids = {
        str(value) for value in excluded_documents.get("audio_chunks", []) or []
    }
    # Early version-2 incrementals recorded inherited chunk IDs but did not carry
    # document digests for them. Keep those archives restorable with an ID-only,
    # projection-only check; current archives take the stronger path below.
    await _verify_collection_documents(
        database,
        "audio_chunks",
        audio_ids - digest_audio_ids,
    )

    for collection_name, values in excluded_documents.items():
        required = {str(value) for value in values or []}
        await _verify_collection_documents(
            database,
            collection_name,
            required,
            expected_digests=digest_index.get(collection_name, {}),
        )

    if not restore_files:
        return
    file_index = manifest.get("data_file_index") or {}
    for member in manifest.get("excluded_data_files") or []:
        destination = _destination_for_data_member(data_dir, member)
        if not destination.is_file():
            raise ArchiveError(
                f"Incremental archive base is incomplete: missing data file {member}"
            )
        sha256, size = _file_digest(destination)
        expected = file_index.get(member) or {}
        if sha256 != expected.get("sha256") or size != expected.get("size"):
            raise ArchiveError(f"Incremental archive base data file changed: {member}")


async def _apply_document_tombstones(database: Any, manifest: dict[str, Any]) -> None:
    for collection_name, values in (manifest.get("deleted_document_ids") or {}).items():
        deleted_ids = {str(value) for value in values or []}
        if not deleted_ids:
            continue
        found = await _collection_documents_by_string_id(
            database, collection_name, deleted_ids
        )
        if found:
            await database[collection_name].delete_many(
                {"_id": {"$in": [document["_id"] for document in found.values()]}}
            )


def _apply_file_tombstones(data_dir: Path, manifest: dict[str, Any]) -> None:
    for member in manifest.get("deleted_data_files") or []:
        _destination_for_data_member(data_dir, member).unlink(missing_ok=True)


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
    progress: Optional[ProgressCallback] = None,
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
    restored_bytes = 0
    total_bytes = sum(
        int(manifest["files"][member].get("size", 0)) for member in members
    )
    _report_progress(
        progress,
        stage="restore_files",
        current=0,
        total=total_bytes,
        unit="bytes",
        detail=f"Restoring {len(members):,} filesystem files",
    )
    for member in members:
        destination = _destination_for_data_member(data_dir, member)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
        try:
            with archive.open(member, mode="r") as source, temp_path.open(
                "wb"
            ) as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
                    restored_bytes += len(chunk)
                    _report_progress(
                        progress,
                        stage="restore_files",
                        current=restored_bytes,
                        total=total_bytes,
                        unit="bytes",
                        detail=member,
                    )
            temp_path.replace(destination)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        restored += 1
    _report_progress(
        progress,
        stage="restore_files",
        current=total_bytes,
        total=total_bytes,
        unit="bytes",
        detail=f"Restored {restored:,} filesystem files",
        completed=True,
    )
    return restored


async def import_data_archive(
    database: Any,
    archive_path: Path,
    *,
    data_dir: Path,
    replace: bool = False,
    restore_files: bool = True,
    fresh_memory: bool = False,
    progress: Optional[ProgressCallback] = None,
    verified_archive: Optional[VerifiedArchive] = None,
) -> ImportSummary:
    """Verify and import an archive, optionally excluding all derived memory state."""
    resolved_archive_path = archive_path.expanduser().resolve()
    if verified_archive is None:
        manifest = verify_data_archive(resolved_archive_path, progress=progress)
    else:
        current = resolved_archive_path.stat()
        if verified_archive.path != resolved_archive_path or (
            verified_archive.size,
            verified_archive.mtime_ns,
        ) != (current.st_size, current.st_mtime_ns):
            raise ArchiveError("Archive changed after checksum verification")
        manifest = verified_archive.manifest
    if fresh_memory and restore_files:
        raise ArchiveError("fresh_memory cannot be combined with restore_files")
    incremental = bool(
        manifest.get("base_archives")
        or manifest.get("excluded_audio_chunk_ids")
        or manifest.get("excluded_document_ids")
        or manifest.get("deleted_document_ids")
        or manifest.get("excluded_data_files")
        or manifest.get("deleted_data_files")
    )
    if replace and incremental:
        raise ArchiveError(
            "Incremental archive cannot be restored with replace=True; restore its "
            "base archive first, then merge this archive"
        )

    if incremental:
        await _verify_incremental_base_presence(
            database,
            manifest,
            data_dir,
            restore_files=restore_files,
        )

    skipped = set(DERIVED_MEMORY_COLLECTIONS if fresh_memory else ())
    restored_collections = 0
    restored_documents = 0
    restored_files = 0
    with zipfile.ZipFile(resolved_archive_path, mode="r", allowZip64=True) as archive:
        restored_document_offset = 0
        total_restore_documents = sum(
            int(metadata.get("documents", 0))
            for collection_name, metadata in manifest["collections"].items()
            if collection_name not in skipped
        )
        _report_progress(
            progress,
            stage="restore_database",
            current=0,
            total=total_restore_documents,
            unit="documents",
            detail="Restoring MongoDB collections",
        )
        for collection_name, metadata in manifest["collections"].items():
            if collection_name in skipped:
                continue
            restored_count = await _restore_collection(
                database,
                archive,
                collection_name,
                metadata["member"],
                metadata["documents"],
                replace=replace,
                progress=progress,
                progress_offset=restored_document_offset,
                progress_total=total_restore_documents,
            )
            restored_document_offset += int(metadata.get("documents", 0))
            restored_documents += restored_count
            restored_collections += 1
        await _apply_document_tombstones(database, manifest)
        _report_progress(
            progress,
            stage="restore_database",
            current=total_restore_documents,
            total=total_restore_documents,
            unit="documents",
            detail=f"Restored {restored_collections:,} MongoDB collections",
            completed=True,
        )
        if restore_files:
            restored_files = _restore_data_files(
                archive,
                manifest,
                data_dir,
                replace=replace,
                progress=progress,
            )
            _apply_file_tombstones(data_dir, manifest)

    return ImportSummary(
        collections=restored_collections,
        documents=restored_documents,
        files=restored_files,
        skipped_collections=tuple(sorted(skipped)),
    )
