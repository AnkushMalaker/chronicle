"""Annotation-dataset export helpers.

Pure helpers shared by the export RQ job (``workers/data_audit_jobs.py``) and
the Data Audit controller: export-id generation/validation, on-disk layout
under ``DATA_DIR/exports/``, and manifest-record construction.

The manifest (one JSONL record per speech clip) is the round-trip contract
with the annotator: the future import endpoint matches records by ``clip_id``
+ ``conversation_id`` + source times and reads the annotator-filled
``annotation`` block.
"""

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from backend.config import DATA_DIR
from backend.models.conversation import Conversation
from backend.utils.transcript_slicing import build_transcript_text

EXPORTS_DIR = DATA_DIR / "exports"

# Export ids are used as filesystem path components — validate strictly.
EXPORT_ID_RE = re.compile(r"^annotation_\d{8}_\d{6}_[a-z0-9]{4}$")

ZIP_NAME = "dataset.zip"
META_NAME = "export.json"
MANIFEST_NAME = "manifest.jsonl"


def new_export_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"annotation_{stamp}_{uuid.uuid4().hex[:4]}"


def validate_export_id(export_id: str) -> bool:
    return bool(EXPORT_ID_RE.match(export_id))


def export_dir(export_id: str) -> Path:
    if not validate_export_id(export_id):
        raise ValueError(f"Invalid export id: {export_id!r}")
    return EXPORTS_DIR / export_id


def active_segments(
    conversation: Conversation,
) -> List["Conversation.SpeakerSegment"]:
    """Segments of the conversation's active transcript version ([] if none)."""
    for version in conversation.transcript_versions:
        if version.version_id == conversation.active_transcript_version:
            return version.segments
    return []


def build_clip_record(
    *,
    conversation_id: str,
    conversation_title: Optional[str],
    client_id: str,
    conversation_created_at: Optional[str],
    clip_index: int,
    region_start: float,
    region_end: float,
    sample_rate: int,
    segments: List["Conversation.SpeakerSegment"],
) -> dict:
    """One manifest.jsonl record for a speech clip.

    ``segments`` must already be clip-relative (sliced with
    ``transcript_slicing.slice_segments``); the clip's absolute position in
    the source conversation is carried by ``source_start_seconds``.
    """
    clip_id = f"{conversation_id}_{clip_index:03d}"
    return {
        "clip_id": clip_id,
        "audio_path": f"audio/{clip_id}.wav",
        "conversation_id": conversation_id,
        "conversation_title": conversation_title,
        "client_id": client_id,
        "conversation_created_at": conversation_created_at,
        "source_start_seconds": round(region_start, 2),
        "source_end_seconds": round(region_end, 2),
        "duration_seconds": round(region_end - region_start, 2),
        "sample_rate": sample_rate,
        "text": build_transcript_text(segments),
        "segments": [
            {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "speaker": seg.speaker,
                "identified_as": seg.identified_as,
                "text": seg.text,
            }
            for seg in segments
        ],
        # Filled by the annotator; read back by the future import endpoint.
        "annotation": {"text": None, "segments": None, "notes": None},
    }
