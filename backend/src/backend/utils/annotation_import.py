"""Read Chronicle annotation dataset ZIPs without extracting them to disk."""

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from backend.utils.annotation_export import MANIFEST_NAME, META_NAME

SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_CLIPS = 500


class AnnotationDatasetError(ValueError):
    """The uploaded ZIP does not satisfy Chronicle's annotation dataset contract."""


@dataclass(frozen=True)
class AnnotationClip:
    clip_id: str
    audio_path: str
    source_conversation_id: str
    conversation_title: str
    source_client_id: str
    transcript: str
    transcript_source: str
    segments: list[dict[str, Any]]
    audio_bytes: bytes
    sample_rate: int
    duration_seconds: float
    notes: str | None


@dataclass(frozen=True)
class AnnotationDataset:
    dataset_id: str
    schema_version: int
    clips: list[AnnotationClip]


def _safe_archive_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnotationDatasetError(f"{field} must be a non-empty path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise AnnotationDatasetError(f"Unsafe {field}: {value!r}")
    return str(path)


def _normalise_segments(
    value: Any, *, clip_id: str, duration_seconds: float
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AnnotationDatasetError(f"Clip {clip_id!r} segments must be a list")

    segments = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} segment {index} must be an object"
            )
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} segment {index} has invalid timing"
            ) from exc
        if start < 0 or end <= start or end > duration_seconds + 0.1:
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} segment {index} falls outside the audio duration"
            )
        text = raw.get("text")
        if not isinstance(text, str):
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} segment {index} text must be a string"
            )
        speaker = raw.get("speaker") or "Unknown Speaker"
        if not isinstance(speaker, str):
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} segment {index} speaker must be a string"
            )
        identified_as = raw.get("identified_as")
        if identified_as is not None and not isinstance(identified_as, str):
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} segment {index} identified_as must be a string or null"
            )
        segments.append(
            {
                "start": start,
                "end": end,
                "speaker": speaker,
                "identified_as": identified_as,
                "text": text,
            }
        )
    return segments


def _active_transcript(
    record: dict[str, Any], duration_seconds: float
) -> tuple[str, list, str]:
    clip_id = record["clip_id"]
    annotation = record.get("annotation") or {}
    if not isinstance(annotation, dict):
        raise AnnotationDatasetError(f"Clip {clip_id!r} annotation must be an object")

    annotation_text = annotation.get("text")
    annotation_segments = annotation.get("segments")
    uses_annotation = annotation_text is not None or annotation_segments is not None

    raw_segments = (
        annotation_segments
        if annotation_segments is not None
        else record.get("segments", [])
    )
    segments = _normalise_segments(
        raw_segments, clip_id=clip_id, duration_seconds=duration_seconds
    )

    if annotation_text is not None:
        if not isinstance(annotation_text, str):
            raise AnnotationDatasetError(
                f"Clip {clip_id!r} annotation.text must be a string or null"
            )
        transcript = annotation_text
        if annotation_segments is None:
            segments = (
                [
                    {
                        "start": 0.0,
                        "end": duration_seconds,
                        "speaker": "Unknown Speaker",
                        "identified_as": None,
                        "text": transcript,
                    }
                ]
                if transcript.strip()
                else []
            )
    elif uses_annotation:
        transcript = " ".join(segment["text"].strip() for segment in segments).strip()
    else:
        transcript = record.get("text", "")
        if not isinstance(transcript, str):
            raise AnnotationDatasetError(f"Clip {clip_id!r} text must be a string")

    return transcript, segments, "human_annotation" if uses_annotation else "manifest"


def parse_annotation_dataset(archive_bytes: bytes) -> AnnotationDataset:
    """Validate and read an export-compatible annotation dataset ZIP."""
    if not archive_bytes:
        raise AnnotationDatasetError("Annotation dataset is empty")
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise AnnotationDatasetError(
            "Annotation dataset exceeds the 512 MB upload limit"
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise AnnotationDatasetError(
            "Annotation dataset must be a valid ZIP file"
        ) from exc

    with archive:
        names = set()
        total_size = 0
        for info in archive.infolist():
            safe_name = _safe_archive_path(info.filename, "ZIP entry")
            if safe_name in names:
                raise AnnotationDatasetError(f"Duplicate ZIP entry: {safe_name}")
            names.add(safe_name)
            total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise AnnotationDatasetError(
                "Annotation dataset expands beyond the 2 GB limit"
            )
        if MANIFEST_NAME not in names:
            raise AnnotationDatasetError(
                f"Annotation dataset is missing {MANIFEST_NAME}"
            )

        manifest_bytes = archive.read(MANIFEST_NAME)
        metadata = {}
        if META_NAME in names:
            try:
                metadata = json.loads(archive.read(META_NAME))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AnnotationDatasetError(f"Invalid {META_NAME}") from exc
            if not isinstance(metadata, dict):
                raise AnnotationDatasetError(f"{META_NAME} must contain an object")

        schema_version = metadata.get("schema_version", SCHEMA_VERSION)
        if schema_version != SCHEMA_VERSION:
            raise AnnotationDatasetError(
                f"Unsupported annotation dataset schema version: {schema_version}"
            )
        dataset_id = metadata.get("export_id")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            dataset_id = (
                f"annotation_import_{hashlib.sha256(manifest_bytes).hexdigest()[:16]}"
            )

        try:
            manifest_text = manifest_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AnnotationDatasetError(f"{MANIFEST_NAME} must be UTF-8") from exc

        clips = []
        seen_clip_ids = set()
        seen_audio_paths = set()
        for line_number, line in enumerate(manifest_text.splitlines(), start=1):
            if not line.strip():
                continue
            if len(clips) >= MAX_CLIPS:
                raise AnnotationDatasetError(
                    f"Annotation dataset exceeds {MAX_CLIPS} clips"
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AnnotationDatasetError(
                    f"Invalid JSON on {MANIFEST_NAME} line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise AnnotationDatasetError(
                    f"{MANIFEST_NAME} line {line_number} must contain an object"
                )

            clip_id = record.get("clip_id")
            if not isinstance(clip_id, str) or not clip_id.strip():
                raise AnnotationDatasetError(
                    f"{MANIFEST_NAME} line {line_number} has no clip_id"
                )
            if clip_id in seen_clip_ids:
                raise AnnotationDatasetError(f"Duplicate clip_id: {clip_id}")
            seen_clip_ids.add(clip_id)

            audio_path = _safe_archive_path(record.get("audio_path"), "audio_path")
            if audio_path in seen_audio_paths:
                raise AnnotationDatasetError(f"Duplicate audio_path: {audio_path}")
            seen_audio_paths.add(audio_path)
            if audio_path not in names:
                raise AnnotationDatasetError(
                    f"Clip {clip_id!r} is missing audio file {audio_path!r}"
                )
            if PurePosixPath(audio_path).suffix.lower() != ".wav":
                raise AnnotationDatasetError(
                    f"Clip {clip_id!r} audio_path must reference a WAV file"
                )

            try:
                duration_seconds = float(record["duration_seconds"])
                sample_rate = int(record["sample_rate"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AnnotationDatasetError(
                    f"Clip {clip_id!r} has invalid duration or sample rate"
                ) from exc
            if duration_seconds <= 0 or sample_rate <= 0:
                raise AnnotationDatasetError(
                    f"Clip {clip_id!r} has invalid duration or sample rate"
                )

            transcript, segments, transcript_source = _active_transcript(
                record, duration_seconds
            )
            annotation = record.get("annotation") or {}
            notes = annotation.get("notes")
            if notes is not None and not isinstance(notes, str):
                raise AnnotationDatasetError(
                    f"Clip {clip_id!r} annotation.notes must be a string or null"
                )
            source_conversation_id = record.get("conversation_id") or clip_id
            if not isinstance(source_conversation_id, str):
                raise AnnotationDatasetError(
                    f"Clip {clip_id!r} conversation_id must be a string"
                )

            clips.append(
                AnnotationClip(
                    clip_id=clip_id,
                    audio_path=audio_path,
                    source_conversation_id=source_conversation_id,
                    conversation_title=str(record.get("conversation_title") or clip_id),
                    source_client_id=str(
                        record.get("client_id") or "annotation-import"
                    ),
                    transcript=transcript,
                    transcript_source=transcript_source,
                    segments=segments,
                    audio_bytes=archive.read(audio_path),
                    sample_rate=sample_rate,
                    duration_seconds=duration_seconds,
                    notes=notes,
                )
            )

        if not clips:
            raise AnnotationDatasetError(f"{MANIFEST_NAME} contains no clips")

    return AnnotationDataset(
        dataset_id=dataset_id,
        schema_version=schema_version,
        clips=clips,
    )
