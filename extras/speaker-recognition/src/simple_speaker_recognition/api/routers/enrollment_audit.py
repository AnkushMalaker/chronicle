"""Enrollment health audit + corrective actions (relabel / delete enrolled clips).

Backs the "Enrollment Health" UI: surfaces contaminated/mislabeled/junk enrolment clips
from the per-clip embeddings, plays them back, and lets the user relabel a clip to the
speaker it actually sounds like or delete it — recomputing the affected centroids so the
fix takes effect on the next identification.
"""

import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import FileResponse
from simple_speaker_recognition.core.enrollment_audit import (
    compute_audit,
    recompute_speaker_centroid,
)
from simple_speaker_recognition.core.unified_speaker_db import UnifiedSpeakerDB
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker, SpeakerAudioSegment

router = APIRouter()
log = logging.getLogger("speaker_service")


async def get_db():
    """Get speaker database dependency."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return await service.get_db()


def get_auth():
    """Get auth settings."""
    # Lazy import: `service` imports this routers package at module load time,
    # so importing it at module level here would create a circular import.
    from .. import service

    return service.auth


def _resolve_seg_path(seg: SpeakerAudioSegment) -> Path:
    """Resolve a segment's audio path, refusing anything outside the enrollment dir."""
    base = get_auth().enrollment_audio_dir.resolve()
    path = (base / seg.audio_file_path).resolve()
    if not str(path).startswith(str(base)):
        raise HTTPException(
            403, "Segment audio path is outside the enrollment directory"
        )
    return path


@router.get("/enrollment/health")
async def enrollment_health(
    user_id: Optional[int] = Query(
        None, description="Scope audit to a user's speakers"
    ),
):
    """Contamination audit over per-clip enrollment embeddings."""
    session = get_db_session()
    try:
        return compute_audit(session, user_id)
    finally:
        session.close()


@router.get("/enrollment/segments/{segment_id}/audio")
async def segment_audio(segment_id: int):
    """Stream a single enrolled clip for playback in the audit UI."""
    session = get_db_session()
    try:
        seg = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not seg:
            raise HTTPException(404, "Segment not found")
        path = _resolve_seg_path(seg)
    finally:
        session.close()

    if not path.exists():
        raise HTTPException(404, "Audio file missing on disk")
    return FileResponse(str(path), media_type="audio/wav", filename=path.name)


@router.post("/enrollment/segments/{segment_id}/relabel")
async def relabel_segment(
    segment_id: int,
    target_speaker_id: str = Form(..., description="Speaker to move this clip to"),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Move a mislabeled clip to the speaker it actually belongs to.

    Moves the audio file into the target speaker's directory, reassigns the segment, and
    recomputes both speakers' centroids.
    """
    session = get_db_session()
    try:
        seg = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not seg:
            raise HTTPException(404, "Segment not found")
        src_id = seg.speaker_id
        if src_id == target_speaker_id:
            raise HTTPException(400, "Segment is already assigned to that speaker")

        target = session.query(Speaker).filter(Speaker.id == target_speaker_id).first()
        if not target:
            raise HTTPException(404, "Target speaker not found")

        base = get_auth().enrollment_audio_dir.resolve()
        old_path = _resolve_seg_path(seg)
        new_dir = base / str(target.user_id) / target_speaker_id
        new_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_dir / old_path.name
        if old_path.exists():
            shutil.move(str(old_path), str(new_path))

        seg.audio_file_path = str(new_path.relative_to(base))
        seg.speaker_id = target_speaker_id
        session.commit()

        recompute_speaker_centroid(session, db, src_id)
        recompute_speaker_centroid(session, db, target_speaker_id)
        log.info(
            "Relabeled segment %s: %s -> %s", segment_id, src_id, target_speaker_id
        )
        return {
            "relabeled": True,
            "segment_id": segment_id,
            "from": src_id,
            "to": target_speaker_id,
        }
    finally:
        session.close()


@router.post("/enrollment/segments/{segment_id}/delete")
async def delete_segment(
    segment_id: int,
    hard: bool = Form(
        False,
        description="Permanently delete the audio file instead of quarantining it",
    ),
    db: UnifiedSpeakerDB = Depends(get_db),
):
    """Remove a junk clip from a speaker's voiceprint.

    By default the audio is **quarantined** — moved out of the enrollment tree into a
    ``quarantine/<speaker_id>/`` folder under the data dir — so it's removed from the
    gallery (and re-averaged out of the centroid) but recoverable. Pass ``hard=true`` to
    delete the file permanently instead. Either way the SpeakerAudioSegment row is
    dropped so the clip no longer counts toward the voiceprint.
    """
    session = get_db_session()
    try:
        seg = (
            session.query(SpeakerAudioSegment)
            .filter(SpeakerAudioSegment.id == segment_id)
            .first()
        )
        if not seg:
            raise HTTPException(404, "Segment not found")
        src_id = seg.speaker_id

        quarantined_to = None
        try:
            path = _resolve_seg_path(seg)
            if path.exists():
                if hard:
                    path.unlink()
                else:
                    quarantine_dir = get_auth().data_dir / "quarantine" / src_id
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    # Prefix with segment id so re-quarantined same-name clips don't clash.
                    dest = quarantine_dir / f"{segment_id}_{path.name}"
                    shutil.move(str(path), str(dest))
                    quarantined_to = str(dest)
        except HTTPException:
            raise
        except Exception as e:
            log.warning("Could not move/delete audio for segment %s: %s", segment_id, e)

        session.delete(seg)
        session.commit()

        recompute_speaker_centroid(session, db, src_id)
        log.info(
            "Removed segment %s from speaker %s (%s)",
            segment_id,
            src_id,
            "hard-deleted" if hard else f"quarantined -> {quarantined_to}",
        )
        return {
            "deleted": True,
            "hard": hard,
            "segment_id": segment_id,
            "speaker_id": src_id,
            "quarantined_to": quarantined_to,
        }
    finally:
        session.close()
