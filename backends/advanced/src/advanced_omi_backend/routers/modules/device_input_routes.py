"""Pairing, ingestion, bounded source jobs, and timeline APIs for capture devices."""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.config import get_screen_context_settings
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    MAX_FRAME_CANDIDATES,
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
    PairingCode,
    utcnow,
)
from advanced_omi_backend.models.timeline import AudioEvidenceSpan, TimelineEpisode
from advanced_omi_backend.models.user import User
from advanced_omi_backend.services.device_context import (
    request_conversation_context_jobs,
    select_context_items,
)
from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager
from advanced_omi_backend.services.memory.vault_media import (
    promote_image_bytes,
    write_media_note,
)
from advanced_omi_backend.workers.screenshot_jobs import enqueue_screenshot_description

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/device-input", tags=["device-input"])
_PAIRING_TTL = timedelta(minutes=10)
_SOURCE_ONLINE_TTL = timedelta(minutes=2)
# Device input is staged in a MongoDB document before it is assembled into a
# Chronicle conversation. Stay below MongoDB's 16 MiB BSON document limit.
_MAX_AUDIO_BYTES = 12 * 1024 * 1024
_ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "video/mp4",  # ScreenPipe audio-only chunks use an MP4 container.
    "audio/ogg",
}
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
# Frames an episode may stage for its picker; mirrors FRAMES_PER_EPISODE.
_MAX_EPISODE_FRAMES = 6
# Shared screenshots are stored inline in the item document, so this has to stay well
# under the 16 MiB BSON limit that _MAX_IMAGE_BYTES above already exceeds. Phone
# screenshots are 200 KB-2 MB, so this is a wide margin, not a tight budget.
_MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
_ALLOWED_SCREENSHOT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_id(user: User) -> str:
    return str(user.user_id)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _effective_source_status(
    source: CaptureSource, now: Optional[datetime] = None
) -> str:
    if source.status != "online":
        return source.status
    # Immich is polled by Chronicle on a schedule; it does not send the frequent
    # heartbeats expected from live ScreenPipe capture agents.  Its last_seen_at
    # therefore means "last successful sync", not "last heartbeat".
    if source.provider == "immich":
        return "online"
    checked_at = _as_utc(now or utcnow())
    if source.last_seen_at is None:
        return "offline"
    if checked_at - _as_utc(source.last_seen_at) > _SOURCE_ONLINE_TTL:
        return "offline"
    return "online"


async def _device_source(authorization: str = Header(default="")) -> CaptureSource:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing device token")
    source = await CaptureSource.find_one(CaptureSource.token_hash == _digest(token))
    if source is None:
        raise HTTPException(status_code=401, detail="Invalid device token")
    return source


async def _mobile_source(user: User) -> CaptureSource:
    """Get or create the synthetic capture source a user's phone shares through.

    The phone already holds a JWT, so it never pairs: pairing would mint a second
    long-lived credential on the same device for no gain. ``token_hash`` is therefore
    deliberately unmatchable — ``_device_source`` only ever looks up a 64-char sha256
    hex digest, and this is not one — while still satisfying the unique index.
    """
    user_id = _user_id(user)
    source_id = f"mobile-{user_id}"
    source = await CaptureSource.find_one(
        CaptureSource.user_id == user_id,
        CaptureSource.source_id == source_id,
    )
    if source is None:
        source = CaptureSource(
            user_id=user_id,
            source_id=source_id,
            name="Phone",
            provider="mobile",
            platform="mobile",
            token_hash=f"unusable:{secrets.token_hex(32)}",
            capabilities=["screenshot"],
        )
        try:
            await source.insert()
        except DuplicateKeyError:
            source = await CaptureSource.find_one(
                CaptureSource.user_id == user_id,
                CaptureSource.source_id == source_id,
            )
            if source is None:
                raise
    # "Last seen" for a share target genuinely means "last share", so the ordinary
    # heartbeat staleness rule in _effective_source_status applies without a special case.
    source.status = "online"
    source.last_seen_at = utcnow()
    await source.save()
    return source


class PairRequest(BaseModel):
    code: str
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)
    provider: Literal["screenpipe", "immich"] = "screenpipe"
    capabilities: list[str] = Field(default_factory=list)


class HeartbeatRequest(BaseModel):
    status: Literal["online", "offline", "error"] = "online"
    health: dict[str, Any] = Field(default_factory=dict)


class ActivityItem(BaseModel):
    source_item_id: str
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityBatch(BaseModel):
    items: list[ActivityItem] = Field(max_length=1000)


class ObservationSample(BaseModel):
    captured_at: datetime
    elapsed_seconds: float = Field(ge=0)
    capture_trigger: str = Field(default="", max_length=100)
    text: str = Field(default="", max_length=2000)
    text_source: Optional[str] = Field(default=None, max_length=50)
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    frame_id: int
    liveness: bool = False
    inactive: bool = False


class ObservationEvent(BaseModel):
    event: Literal["open", "sample", "close"]
    source_item_id: str = Field(min_length=1, max_length=200)
    captured_at: datetime
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    frame_candidates: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_FRAME_CANDIDATES
    )
    sample: Optional[ObservationSample] = None


class ObservationBatch(BaseModel):
    events: list[ObservationEvent] = Field(min_length=1, max_length=1000)


class JobRequest(BaseModel):
    source_id: str
    kind: Literal["screen_context", "thumbnail", "source_media"] = "screen_context"
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    purpose: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobCompletion(BaseModel):
    success: bool = True
    items: list[ActivityItem] = Field(default_factory=list)
    error: Optional[str] = None


@router.post("/pairing-codes")
async def create_pairing_code(user: User = Depends(current_active_user)):
    raw = secrets.token_urlsafe(9)
    expires_at = utcnow() + _PAIRING_TTL
    await PairingCode(
        user_id=_user_id(user), code_hash=_digest(raw), expires_at=expires_at
    ).insert()
    return {"code": raw, "expires_at": _utc_iso(expires_at)}


@router.post("/pair")
async def pair_source(body: PairRequest):
    code = await PairingCode.find_one(PairingCode.code_hash == _digest(body.code))
    if code is None or _as_utc(code.expires_at) <= utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code")
    if body.provider not in {"screenpipe", "immich"}:
        raise HTTPException(status_code=422, detail="Unsupported provider")
    raw_token = secrets.token_urlsafe(32)
    source_id = f"{body.provider}-{secrets.token_hex(8)}"
    source = CaptureSource(
        user_id=code.user_id,
        source_id=source_id,
        name=body.name,
        provider=body.provider,
        platform=body.platform,
        capabilities=body.capabilities,
        token_hash=_digest(raw_token),
        status="online",
        last_seen_at=utcnow(),
    )
    await source.insert()
    await code.delete()
    return {"source_id": source_id, "token": raw_token}


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatRequest, source: CaptureSource = Depends(_device_source)
):
    source.status = (
        body.status if body.status in {"online", "offline", "error"} else "error"
    )
    source.health = body.health
    source.last_seen_at = utcnow()
    await source.save()
    return {"ok": True, "source_id": source.source_id}


@router.post("/activity")
async def ingest_activity(
    body: ActivityBatch, source: CaptureSource = Depends(_device_source)
):
    accepted, duplicates = 0, 0
    for incoming in body.items:
        item = DeviceInputItem(
            user_id=source.user_id,
            source_id=source.source_id,
            kind="activity",
            source_item_id=incoming.source_item_id,
            captured_at=incoming.captured_at,
            ended_at=incoming.ended_at,
            metadata=incoming.metadata,
        )
        try:
            await item.insert()
            accepted += 1
            frame_id = incoming.metadata.get("representative_frame_id")
            if (
                frame_id is not None
                and "screen_context" in source.capabilities
                and any(
                    incoming.metadata.get(key)
                    for key in ("app_name", "window_name", "text")
                )
            ):
                await DeviceInputJob(
                    user_id=source.user_id,
                    source_id=source.source_id,
                    kind="thumbnail",
                    start_at=incoming.captured_at,
                    end_at=incoming.ended_at,
                    purpose="timeline_thumbnail",
                    payload={
                        "item_id": str(item.id),
                        "frame_id": frame_id,
                        "width": 960,
                    },
                ).insert()
        except DuplicateKeyError:
            duplicates += 1
            existing = await DeviceInputItem.find_one(
                DeviceInputItem.user_id == source.user_id,
                DeviceInputItem.source_id == source.source_id,
                DeviceInputItem.kind == "activity",
                DeviceInputItem.source_item_id == incoming.source_item_id,
            )
            if existing is not None:
                existing.ended_at = incoming.ended_at
                existing.metadata = incoming.metadata
                await existing.save()
    return {"accepted": accepted, "duplicates": duplicates}


def _frame_id_from_filename(filename: str | None) -> int | None:
    """Read the frame id out of a ``frame-<id>.<ext>`` upload name."""

    match = re.fullmatch(r"frame-(\d+)\.[A-Za-z0-9]+", filename or "")
    return int(match.group(1)) if match else None


def _candidate_seconds(candidate: dict[str, Any]) -> float:
    """Capture time of a candidate; frames without one sort to the head."""

    raw = candidate.get("captured_at")
    if isinstance(raw, datetime):
        return _as_utc(raw).timestamp()
    if isinstance(raw, str):
        try:
            return _as_utc(
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
            ).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _merge_frame_candidates(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union two shortlists, keeping one frame per slice of the observation's span.

    Truncating the union by score would undo the collector's stratification on every
    sample append: an observation that accumulates candidates over hours would end up
    holding whichever few frames happened to score highest, which are typically
    neighbours. Bucketing by capture time keeps the shortlist spanning the session.
    """

    by_frame: dict[int, dict[str, Any]] = {}
    for candidate in [*existing, *incoming]:
        frame_id = candidate.get("frame_id")
        if not isinstance(frame_id, int):
            continue
        current = by_frame.get(frame_id)
        if current is None or float(candidate.get("score") or 0) > float(
            current.get("score") or 0
        ):
            by_frame[frame_id] = candidate
    if len(by_frame) <= MAX_FRAME_CANDIDATES:
        return sorted(by_frame.values(), key=lambda candidate: candidate["frame_id"])
    moments = {
        frame_id: _candidate_seconds(candidate)
        for frame_id, candidate in by_frame.items()
    }
    start, end = min(moments.values()), max(moments.values())
    width = (end - start) / MAX_FRAME_CANDIDATES or 1.0
    best: dict[int, dict[str, Any]] = {}
    for frame_id, candidate in by_frame.items():
        bucket = min(int((moments[frame_id] - start) / width), MAX_FRAME_CANDIDATES - 1)
        current = best.get(bucket)
        if current is None or float(candidate.get("score") or 0) > float(
            current.get("score") or 0
        ):
            best[bucket] = candidate
    return sorted(best.values(), key=lambda candidate: candidate["frame_id"])


def _append_observation_sample(
    item: DeviceInputItem, sample: ObservationSample | None
) -> bool:
    if sample is None:
        return False
    incoming = sample.model_dump(mode="json")
    identity = (incoming["content_fingerprint"], incoming["captured_at"])
    if any(
        (row.get("content_fingerprint"), row.get("captured_at")) == identity
        for row in item.samples
    ):
        return False
    item.samples.append(incoming)
    item.samples.sort(key=lambda row: row["captured_at"])
    return True


def _observation_has_visual_context(item: DeviceInputItem) -> bool:
    if item.metadata.get("inactive") or not item.frame_candidates:
        return False
    latest_text = item.samples[-1].get("text") if item.samples else ""
    return bool(
        latest_text
        or item.metadata.get("app_name")
        or item.metadata.get("window_name")
        or item.metadata.get("browser_url")
    )


async def _ensure_observation_preview(
    item: DeviceInputItem, source: CaptureSource
) -> None:
    if (
        "screen_context" not in source.capabilities
        or item.media_data
        or not _observation_has_visual_context(item)
    ):
        return
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.kind == "thumbnail",
        {"payload.item_id": str(item.id)},
        {"status": {"$in": ["pending", "claimed", "complete"]}},
    )
    if existing:
        return
    frame_id = item.frame_candidates[0]["frame_id"]
    await DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="thumbnail",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="observation_preview",
        payload={
            "item_id": str(item.id),
            "frame_id": frame_id,
            "width": 960,
            "preview_index": 1,
        },
    ).insert()


@router.post("/observations")
async def ingest_observations(
    body: ObservationBatch, source: CaptureSource = Depends(_device_source)
):
    accepted = 0
    duplicate_samples = 0
    touched: dict[str, DeviceInputItem] = {}
    for incoming in body.events:
        item = await DeviceInputItem.find_one(
            DeviceInputItem.user_id == source.user_id,
            DeviceInputItem.source_id == source.source_id,
            DeviceInputItem.kind == "observation",
            DeviceInputItem.source_item_id == incoming.source_item_id,
        )
        if item is None:
            if incoming.event != "open":
                raise HTTPException(
                    status_code=409,
                    detail=f"Observation {incoming.source_item_id} must be opened first",
                )
            item = DeviceInputItem(
                user_id=source.user_id,
                source_id=source.source_id,
                kind="observation",
                source_item_id=incoming.source_item_id,
                captured_at=incoming.captured_at,
                ended_at=incoming.ended_at,
                metadata=incoming.metadata,
                lifecycle="open",
                curation="pending",
                frame_candidates=incoming.frame_candidates,
            )
            _append_observation_sample(item, incoming.sample)
            try:
                await item.insert()
                accepted += 1
            except DuplicateKeyError:
                item = await DeviceInputItem.find_one(
                    DeviceInputItem.user_id == source.user_id,
                    DeviceInputItem.source_id == source.source_id,
                    DeviceInputItem.kind == "observation",
                    DeviceInputItem.source_item_id == incoming.source_item_id,
                )
                if item is None:
                    raise
        else:
            item.metadata = {**item.metadata, **incoming.metadata}
            item.frame_candidates = _merge_frame_candidates(
                item.frame_candidates, incoming.frame_candidates
            )
            if incoming.ended_at is not None:
                item.ended_at = incoming.ended_at
            if _append_observation_sample(item, incoming.sample):
                item.curation = "pending"
                accepted += 1
            elif incoming.sample is not None:
                duplicate_samples += 1
            if incoming.event == "close":
                item.lifecycle = "closed"
                item.ended_at = incoming.ended_at or incoming.captured_at
                item.curation = "pending"
            await item.save()
        touched[str(item.id)] = item

    for item in touched.values():
        await _ensure_observation_preview(item, source)
    source.last_seen_at = utcnow()
    source.status = "online"
    await source.save()
    return {
        "accepted": accepted,
        "duplicate_samples": duplicate_samples,
        "observations": list(touched),
    }


@router.post("/audio")
async def ingest_audio(
    file: UploadFile = File(...),
    source_item_id: str = Form(...),
    captured_at: datetime = Form(...),
    duration_seconds: float = Form(..., ge=0),
    device_name: str = Form(...),
    direction: Literal["input", "output", "unknown"] = Form(...),
    content_hash: str = Form(..., pattern=r"^[0-9a-fA-F]{64}$"),
    meeting_id: Optional[str] = Form(None, min_length=1, max_length=200),
    source: CaptureSource = Depends(_device_source),
):
    existing = await DeviceInputItem.find_one(
        DeviceInputItem.user_id == source.user_id,
        DeviceInputItem.source_id == source.source_id,
        DeviceInputItem.kind == "audio",
        DeviceInputItem.source_item_id == source_item_id,
    )
    if existing:
        return {"status": "duplicate", "item_id": str(existing.id)}
    compacted = await AudioEvidenceSpan.find_one(
        AudioEvidenceSpan.user_id == source.user_id,
        AudioEvidenceSpan.source_id == source.source_id,
        {"source_item_ids": source_item_id},
    )
    if compacted:
        return {"status": "duplicate", "compacted": True}
    if file.content_type not in _ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    data = await file.read(_MAX_AUDIO_BYTES + 1)
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio chunk is too large")
    actual_hash = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_hash, content_hash.lower()):
        raise HTTPException(status_code=422, detail="Content hash mismatch")
    item = DeviceInputItem(
        user_id=source.user_id,
        source_id=source.source_id,
        kind="audio",
        source_item_id=source_item_id,
        captured_at=captured_at,
        ended_at=captured_at + timedelta(seconds=duration_seconds),
        metadata={
            "device_name": device_name,
            "direction": direction,
            "duration_seconds": duration_seconds,
            **({"meeting_id": meeting_id} if meeting_id else {}),
        },
        media_data=data,
        media_filename=file.filename or "chunk.wav",
        media_content_type=file.content_type,
        content_hash=actual_hash,
    )
    try:
        await item.insert()
    except DuplicateKeyError:
        return {"status": "duplicate"}
    return {"status": "accepted", "item_id": str(item.id)}


@router.get("/jobs/next")
async def next_job(source: CaptureSource = Depends(_device_source)):
    await DeviceInputJob.find(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.status == "claimed",
        DeviceInputJob.claimed_at < utcnow() - timedelta(minutes=5),
    ).update_many({"$set": {"status": "pending", "claimed_at": None}})
    job = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == source.source_id,
        DeviceInputJob.status == "pending",
        sort=[("created_at", 1)],
    )
    if job is None:
        return {"job": None}
    job.status = "claimed"
    job.claimed_at = utcnow()
    await job.save()
    return {
        "job": {
            "id": str(job.id),
            "kind": job.kind,
            "start_at": job.start_at,
            "end_at": job.end_at,
            "purpose": job.purpose,
            "payload": job.payload,
        }
    }


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str, body: JobCompletion, source: CaptureSource = Depends(_device_source)
):
    job = await DeviceInputJob.get(job_id)
    if job is None or job.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Job not found")
    settings = get_screen_context_settings()
    kept, report = select_context_items(
        body.items,
        max_bytes=settings["max_bytes_per_conversation"],
        similarity_threshold=settings["similarity_threshold"],
    )
    conversation_id = job.payload.get("conversation_id")
    for incoming in kept:
        try:
            await DeviceInputItem(
                user_id=source.user_id,
                source_id=source.source_id,
                kind="screen_context",
                source_item_id=incoming.source_item_id,
                captured_at=incoming.captured_at,
                ended_at=incoming.ended_at,
                metadata={
                    **incoming.metadata,
                    "job_id": job_id,
                    "purpose": job.purpose,
                },
                conversation_id=conversation_id,
                state="linked" if conversation_id else "received",
            ).insert()
        except DuplicateKeyError:
            pass
    if report["over_budget"]:
        logger.warning(
            "Screen-context job %s hit the %d-byte budget; %d frames not stored",
            job_id,
            settings["max_bytes_per_conversation"],
            report["over_budget"],
        )
    job.status = "complete" if body.success else "failed"
    job.error = body.error
    job.completed_at = utcnow()
    job.payload = {
        **job.payload,
        "items_received": len(body.items),
        "items_stored": len(kept),
        "items_filtered": report,
    }
    await job.save()
    return {"ok": True, "stored": len(kept), "filtered": report}


async def _store_episode_frames(
    job: DeviceInputJob, episode_id: str, files: list[UploadFile]
) -> dict[str, Any]:
    """Stage frames sampled across an episode's interval for its picker.

    Unlike an observation shortlist there is no pre-agreed set of ids to validate
    against — the node resolved these from its own frame store — so the filename is
    read for the id and nothing else is assumed.
    """

    episode = await TimelineEpisode.find_one(
        TimelineEpisode.episode_id == episode_id,
        TimelineEpisode.user_id == job.user_id,
    )
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    frames: list[dict[str, Any]] = []
    for upload in files[:_MAX_EPISODE_FRAMES]:
        content_type = (upload.content_type or "").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Frames must be images")
        frame_id = _frame_id_from_filename(upload.filename)
        if frame_id is None:
            raise HTTPException(
                status_code=422, detail="Frame filename must name an id"
            )
        data = await upload.read(_MAX_IMAGE_BYTES + 1)
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Frame exceeds the media limit")
        frames.append(
            {"frame_id": frame_id, "data": data, "content_type": content_type}
        )
    episode.frame_shortlist = sorted(frames, key=lambda frame: frame["frame_id"])
    # An interval the recorder never covered yields nothing; say so rather than
    # leaving the episode waiting on a request that already came back empty.
    episode.thumbnail_state = "requested" if frames else "unavailable"
    await episode.save()
    job.status = "complete"
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True, "episode_id": episode_id, "frames": len(frames)}


@router.post("/jobs/{job_id}/previews")
async def complete_preview_batch_job(
    job_id: str,
    files: list[UploadFile] = File(...),
    source: CaptureSource = Depends(_device_source),
):
    """Store the shortlist of frames the curation agent will choose between.

    One request carries every frame of one observation, so a shortlist costs a single
    node round trip instead of one job per frame. This deliberately does not set
    ``media_data``: which frame represents the observation is the agent's decision,
    made after looking at them, not the scorer's.

    Each filename must be ``frame-<frame_id>.<ext>`` — that is how a stored preview is
    tied back to the candidate it came from.
    """

    job = await DeviceInputJob.get(job_id)
    if job is None or job.source_id != source.source_id or job.kind != "thumbnail":
        raise HTTPException(status_code=404, detail="Preview job not found")
    episode_id = job.payload.get("episode_id")
    if episode_id:
        return await _store_episode_frames(job, episode_id, files)
    item_id = job.payload.get("item_id")
    item = await DeviceInputItem.get(item_id) if item_id else None
    if item is None or item.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Timeline item not found")
    requested = {int(frame_id) for frame_id in job.payload.get("frame_ids") or []}
    captured_at = {
        int(candidate["frame_id"]): candidate.get("captured_at")
        for candidate in item.frame_candidates
        if isinstance(candidate.get("frame_id"), int)
    }
    previews: list[dict[str, Any]] = []
    for upload in files[:MAX_FRAME_CANDIDATES]:
        content_type = (upload.content_type or "").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Previews must be images")
        frame_id = _frame_id_from_filename(upload.filename)
        if frame_id is None or frame_id not in requested:
            raise HTTPException(
                status_code=422, detail="Preview filename must name a requested frame"
            )
        data = await upload.read(_MAX_IMAGE_BYTES + 1)
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413, detail="Preview exceeds the media limit"
            )
        previews.append(
            {
                "frame_id": frame_id,
                "data": data,
                "content_type": content_type,
                "captured_at": captured_at.get(frame_id),
            }
        )
    item.media_previews = sorted(previews, key=lambda preview: preview["frame_id"])
    item.metadata = {
        **item.metadata,
        "preview_shortlist_count": len(previews),
        # Frames that could not be served are gone from ScreenPipe's store for good.
        # Recording the shortfall stops the curation gate waiting on them.
        "preview_shortlist_missing": sorted(
            requested - {preview["frame_id"] for preview in previews}
        ),
    }
    await item.save()
    job.status = "complete"
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True, "item_id": str(item.id), "previews": len(previews)}


@router.post("/jobs/{job_id}/thumbnail")
async def complete_thumbnail_job(
    job_id: str,
    file: UploadFile = File(...),
    source: CaptureSource = Depends(_device_source),
):
    job = await DeviceInputJob.get(job_id)
    if (
        job is None
        or job.source_id != source.source_id
        or job.kind not in {"thumbnail", "source_media"}
    ):
        raise HTTPException(status_code=404, detail="Thumbnail job not found")
    item_id = job.payload.get("item_id")
    item = await DeviceInputItem.get(item_id) if item_id else None
    if item is None or item.source_id != source.source_id:
        raise HTTPException(status_code=404, detail="Timeline item not found")
    content_type = (file.content_type or "").split(";", 1)[0]
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Thumbnail must be an image")
    data = await file.read(_MAX_IMAGE_BYTES + 1)
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Thumbnail exceeds the media limit")
    item.media_data = data
    item.media_filename = file.filename or "screenpipe-thumbnail.jpg"
    item.media_content_type = content_type
    item.content_hash = hashlib.sha256(data).hexdigest()
    item.metadata = {
        **item.metadata,
        "thumbnail_available": True,
        "preview_frame_id": job.payload.get("frame_id"),
        "preview_count": max(
            int(item.metadata.get("preview_count") or 0),
            int(job.payload.get("preview_index") or 1),
        ),
        "source_media_available": job.kind == "source_media"
        or bool(item.metadata.get("source_media_available")),
    }
    await item.save()
    job.status = "complete"
    job.completed_at = utcnow()
    await job.save()
    return {"ok": True, "item_id": str(item.id)}


@router.post("/screenshots", status_code=202)
async def ingest_screenshot(
    file: UploadFile = File(...),
    captured_at: Optional[datetime] = Form(default=None),
    caption: Optional[str] = Form(default=None),
    origin_app: Optional[str] = Form(default=None),
    user: User = Depends(current_active_user),
):
    """Accept one screenshot shared from the user's phone.

    Push, not pull: every other image path in this router mints a job that a capture
    node answers later from its own frame store. A phone holds the bytes once and can
    never serve them again, so it uploads directly and this returns as soon as the
    image is durable. Describing and indexing it happen afterwards.
    """
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in _ALLOWED_SCREENSHOT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Screenshots must be JPEG, PNG, or WebP; convert before uploading",
        )
    data = await file.read(_MAX_SCREENSHOT_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty screenshot upload")
    if len(data) > _MAX_SCREENSHOT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Screenshot exceeds {_MAX_SCREENSHOT_BYTES // (1024 * 1024)} MiB; "
                "downscale and retry"
            ),
        )
    if caption and len(caption) > 2000:
        raise HTTPException(status_code=422, detail="Caption exceeds 2000 characters")

    user_id = _user_id(user)
    source = await _mobile_source(user)
    digest = hashlib.sha256(data).hexdigest()

    # The vault copy is written before the document so a durable, content-addressed
    # original exists even if every downstream stage fails forever.
    root = ConvDocVaultManager().user_root(user_id)
    media_path, _ = await asyncio.to_thread(
        promote_image_bytes, data, content_type, root
    )

    item = DeviceInputItem(
        user_id=user_id,
        source_id=source.source_id,
        kind="screenshot",
        source_item_id=digest,
        captured_at=_as_utc(captured_at) if captured_at else utcnow(),
        metadata={
            "caption": (caption or "").strip() or None,
            "origin_app": (origin_app or "").strip() or None,
            "shared_at": _utc_iso(utcnow()),
            "description_state": "pending",
            "description_attempts": 0,
            "embed_state": "pending",
            "embed_attempts": 0,
        },
        media_data=data,
        media_filename=file.filename or f"{digest[:12]}.{content_type.split('/')[-1]}",
        media_content_type=content_type,
        content_hash=digest,
        promoted_path=media_path,
    )
    try:
        await item.insert()
    except DuplicateKeyError:
        # The unique (user_id, source_id, kind, source_item_id) index makes a re-share
        # or a client retry idempotent without any client-supplied dedupe token.
        existing = await DeviceInputItem.find_one(
            DeviceInputItem.user_id == user_id,
            DeviceInputItem.source_id == source.source_id,
            DeviceInputItem.kind == "screenshot",
            DeviceInputItem.source_item_id == digest,
        )
        if existing is None:
            raise
        return {
            "status": "duplicate",
            "item_id": str(existing.id),
            "content_hash": digest,
        }

    enqueue_screenshot_description(str(item.id))
    return {"status": "accepted", "item_id": str(item.id), "content_hash": digest}


@router.get("/sources")
async def list_sources(user: User = Depends(current_active_user)):
    rows = (
        await CaptureSource.find(CaptureSource.user_id == _user_id(user))
        .sort("-last_seen_at")
        .to_list()
    )
    return {
        "sources": [
            {
                "source_id": row.source_id,
                "name": row.name,
                "provider": row.provider,
                "platform": row.platform,
                "status": _effective_source_status(row),
                "health": row.health,
                "last_seen_at": _utc_iso(row.last_seen_at),
                "capabilities": row.capabilities,
            }
            for row in rows
        ]
    }


@router.post("/jobs")
async def create_job(body: JobRequest, user: User = Depends(current_active_user)):
    source = await CaptureSource.find_one(
        CaptureSource.user_id == _user_id(user),
        CaptureSource.source_id == body.source_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    job = DeviceInputJob(
        user_id=_user_id(user),
        source_id=body.source_id,
        kind=body.kind,
        start_at=body.start_at,
        end_at=body.end_at,
        purpose=body.purpose,
        payload=body.payload,
    )
    await job.insert()
    return {"job_id": str(job.id), "status": job.status}


@router.get("/timeline")
async def timeline(
    start_at: datetime, end_at: datetime, user: User = Depends(current_active_user)
):
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == _user_id(user),
            DeviceInputItem.captured_at <= end_at,
            {"$or": [{"ended_at": None}, {"ended_at": {"$gte": start_at}}]},
        )
        .sort("captured_at")
        .to_list()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "source_id": row.source_id,
                "kind": row.kind,
                "source_item_id": row.source_item_id,
                "captured_at": _utc_iso(row.captured_at),
                "ended_at": _utc_iso(row.ended_at),
                "metadata": row.metadata,
                "state": row.state,
                "lifecycle": row.lifecycle,
                "curation": row.curation,
                "samples": row.samples,
                "frame_candidates": row.frame_candidates,
                "related_conversation_ids": row.related_conversation_ids,
                "duplicate_of": row.duplicate_of,
                "agent_reason": row.agent_reason,
                "vault_paths": row.vault_paths,
            }
            for row in rows
        ]
    }


@router.get("/conversations/{conversation_id}/context")
async def conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    rows = (
        await DeviceInputItem.find(
            DeviceInputItem.user_id == _user_id(user),
            DeviceInputItem.conversation_id == conversation_id,
        )
        .sort("captured_at")
        .to_list()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "source_id": row.source_id,
                "kind": row.kind,
                "captured_at": _utc_iso(row.captured_at),
                "ended_at": _utc_iso(row.ended_at),
                "metadata": row.metadata,
                "state": row.state,
            }
            for row in rows
        ]
    }


@router.post("/conversations/{conversation_id}/request-context")
async def request_conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    owner = _user_id(user)
    conversation = await Conversation.find_one(
        Conversation.conversation_id == conversation_id,
        Conversation.user_id == owner,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    jobs = await request_conversation_context_jobs(conversation)
    return {"jobs": jobs}


@router.delete("/conversations/{conversation_id}/context")
async def clear_conversation_context(
    conversation_id: str, user: User = Depends(current_active_user)
):
    result = await DeviceInputItem.find(
        DeviceInputItem.user_id == _user_id(user),
        DeviceInputItem.conversation_id == conversation_id,
        DeviceInputItem.state != "promoted",
    ).update_many({"$set": {"conversation_id": None, "state": "received"}})
    return {"cleared": result.modified_count}


async def _owned_item(item_id: str, user: User) -> DeviceInputItem:
    try:
        item = await DeviceInputItem.get(item_id)
    except Exception:
        item = None
    if item is None or item.user_id != _user_id(user):
        raise HTTPException(status_code=404, detail="Context item not found")
    return item


async def _immich_bytes(asset_id: str, endpoint: str) -> tuple[bytes, str]:
    base = os.getenv("IMMICH_URL", "").rstrip("/")
    key = os.getenv("IMMICH_API_KEY", "")
    if not base or not key:
        raise HTTPException(status_code=503, detail="Immich is not configured")
    async with httpx.AsyncClient(
        timeout=60, headers={"x-api-key": key, "Accept": "image/*"}
    ) as client:
        response = await client.get(f"{base}/api/assets/{asset_id}/{endpoint}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Source asset is unavailable")
        response.raise_for_status()
        if len(response.content) > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413, detail="Source image exceeds the media limit"
            )
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[
            0
        ]
        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=415, detail="Source did not return an image"
            )
        return response.content, content_type


@router.get("/media/{digest}/thumbnail")
async def media_thumbnail(digest: str, user: User = Depends(current_active_user)):
    """Serve a stored image by its content hash.

    Vault notes are named ``Media/<digest>.md`` and the memory search cites those
    paths, so a client holding a citation already knows the digest but not the item
    id. Addressing by content lets a chat message render its own screenshots without
    the search path having to carry an extra identifier through to the UI.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
        raise HTTPException(status_code=400, detail="Malformed content hash")
    item = await DeviceInputItem.find_one(
        DeviceInputItem.user_id == _user_id(user),
        DeviceInputItem.content_hash == digest.lower(),
    )
    if item is None or not item.media_data:
        raise HTTPException(status_code=404, detail="No stored image for that hash")
    return Response(
        content=item.media_data,
        media_type=item.media_content_type or "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/items/{item_id}/thumbnail")
async def context_thumbnail(item_id: str, user: User = Depends(current_active_user)):
    item = await _owned_item(item_id, user)
    if (
        item.media_data
        and item.media_content_type
        and item.media_content_type.startswith("image/")
    ):
        return Response(
            content=item.media_data,
            media_type=item.media_content_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )
    asset_id = item.metadata.get("asset_id")
    if item.kind != "immich_memory" or not asset_id:
        raise HTTPException(
            status_code=409, detail="This source does not expose an immediate thumbnail"
        )
    data, content_type = await _immich_bytes(str(asset_id), "thumbnail?size=thumbnail")
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/items/{item_id}/request-thumbnail")
async def request_item_thumbnail(
    item_id: str, user: User = Depends(current_active_user)
):
    item = await _owned_item(item_id, user)
    if item.kind not in {"activity", "observation"}:
        raise HTTPException(
            status_code=409, detail="Only screen items have source frames"
        )
    if item.media_data:
        return {"status": "complete"}
    frame_id = (
        item.metadata.get("representative_frame_id")
        or item.metadata.get("last_frame_id")
        or item.metadata.get("first_frame_id")
    )
    if frame_id is None:
        raise HTTPException(status_code=409, detail="Activity has no source frame")
    existing = await DeviceInputJob.find_one(
        DeviceInputJob.source_id == item.source_id,
        DeviceInputJob.kind == "thumbnail",
        {"payload.item_id": item_id},
        {"status": {"$in": ["pending", "claimed"]}},
    )
    if existing:
        return {"status": existing.status, "job_id": str(existing.id)}
    job = DeviceInputJob(
        user_id=item.user_id,
        source_id=item.source_id,
        kind="thumbnail",
        start_at=item.captured_at,
        end_at=item.ended_at,
        purpose="timeline_thumbnail",
        payload={"item_id": item_id, "frame_id": frame_id, "width": 960},
    )
    await job.insert()
    return {"status": "pending", "job_id": str(job.id)}


@router.post("/items/{item_id}/promote")
async def promote_context_item(item_id: str, user: User = Depends(current_active_user)):
    item = await _owned_item(item_id, user)
    if item.promoted_path:
        return {"status": "duplicate", "path": item.promoted_path}
    asset_id = item.metadata.get("asset_id")
    if item.kind != "immich_memory" or not asset_id:
        raise HTTPException(
            status_code=409,
            detail="Source-media retrieval is not available for this item",
        )
    data, content_type = await _immich_bytes(str(asset_id), "original")
    root = ConvDocVaultManager().user_root(_user_id(user))
    try:
        media_path, digest = await asyncio.to_thread(
            promote_image_bytes, data, content_type, root
        )
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    note_path = await asyncio.to_thread(
        write_media_note,
        media_path,
        digest,
        root,
        frontmatter={
            "source": "immich",
            "asset_id": asset_id,
            "captured_at": item.captured_at.isoformat(),
        },
    )
    item.promoted_path = media_path
    item.state = "promoted"
    await item.save()
    return {"status": "promoted", "path": item.promoted_path, "note": note_path}
