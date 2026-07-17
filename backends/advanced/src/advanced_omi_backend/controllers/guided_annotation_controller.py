"""Guided annotation — active-learning selection of conversation windows to
ground-truth for speaker identity and boundaries.

The guided-enrollment sibling: enrollment picks *clips for one speaker's
gallery*; this picks *conversation windows for human annotation* (speaker
boundaries + identities in the transcript editor). The goal is the most
model-information per minute of annotation effort, so windows are ranked by a
per-segment informativeness sum normalized by window duration.

Per-segment informativeness (stored transcript data only — no audio pass):
  * label uncertainty — unlabeled segments carry maximal label entropy;
    attributed segments score by proximity of the stored cosine confidence to
    the operating threshold (the decision boundary, where a human label
    resolves the most uncertainty),
  * shortness — sub-4s segments are the pipeline's dominant failure regime
    (short-duration verification error grows ~2.4x at 2s), so confirming them
    is worth more than confirming long clear turns,
  * overlap — segments overlapping a different speaker mark boundary regions
    where diarization ground truth is scarcest.

Windows already covered by human annotations are down-weighted, decided
windows are never re-suggested (``annotation_review_targets``), and batches
round-robin across speaker signatures so one frequent pair cannot monopolize
the queue (session/pair diversity beats more-of-the-same, as in enrollment).
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from advanced_omi_backend.config import get_diarization_settings
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

MIN_WINDOW_SECONDS = 30.0
MAX_WINDOW_SECONDS = 300.0
WINDOW_STEP_SECONDS = 30.0
MAX_WINDOWS_PER_CONVERSATION = 2
UNCERTAINTY_BAND = 0.25
SHORT_SEGMENT_CAP = 4.0
MIN_WINDOW_SEGMENTS = 3

W_LABEL, W_SHORT, W_OVERLAP = 0.55, 0.25, 0.20

HUMAN_ANNOTATION_TYPES = ["diarization", "timing", "insert", "deletion"]


def _targets_collection():
    return Conversation.get_pymongo_collection().database["annotation_review_targets"]


def _annotations_collection():
    return Conversation.get_pymongo_collection().database["annotations"]


def _target_key(conversation_id: str, window_start: float) -> str:
    return f"{conversation_id}:{round(window_start, 1)}"


def _active_segments(doc: dict) -> list:
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    active = next((v for v in versions if v.get("version_id") == active_id), None)
    if active is None and versions:
        active = versions[-1]
    return (active or {}).get("segments") or []


def _segment_info(seg: dict, segments: list, threshold: float) -> Optional[dict]:
    """Informativeness of confirming one segment's speaker, or None for
    segments a human label teaches us nothing new about."""
    if seg.get("segment_type") not in (None, "speech"):
        return None
    start = float(seg.get("start") or 0.0)
    end = float(seg.get("end") or 0.0)
    duration = end - start
    if duration <= 0:
        return None

    identified = seg.get("identified_as")
    confidence = seg.get("confidence")
    if identified is None or confidence is None:
        label_info = 1.0  # unlabeled: maximal label entropy
    else:
        label_info = 1.0 - min(1.0, abs(confidence - threshold) / UNCERTAINTY_BAND)

    shortness = 1.0 - min(duration, SHORT_SEGMENT_CAP) / SHORT_SEGMENT_CAP

    overlap = 0.0
    for other in segments:
        if other is seg or other.get("segment_type") not in (None, "speech"):
            continue
        if other.get("identified_as") == identified and identified is not None:
            continue
        if other.get("start", 0) < end and start < other.get("end", 0):
            overlap = 1.0
            break

    score = W_LABEL * label_info + W_SHORT * shortness + W_OVERLAP * overlap
    return {
        "start": start,
        "end": end,
        "duration": duration,
        "identified_as": identified,
        "score": score,
        "unlabeled": identified is None,
        "uncertain": identified is not None and label_info >= 0.6,
        "short": duration < 2.0,
        "overlap": overlap > 0,
    }


def _conversation_windows(doc: dict, threshold: float, window_seconds: float) -> list:
    """Best non-overlapping annotation windows of one conversation, scored by
    informativeness per minute of annotation effort."""
    segments = _active_segments(doc)
    infos = [
        info
        for seg in segments
        if (info := _segment_info(seg, segments, threshold)) is not None
    ]
    if len(infos) < MIN_WINDOW_SEGMENTS:
        return []
    audio_end = max(
        float(doc.get("audio_total_duration") or 0.0),
        max(i["end"] for i in infos),
    )

    candidates = []
    start = 0.0
    while start < audio_end:
        end = min(start + window_seconds, audio_end)
        inside = [i for i in infos if i["start"] < end and i["end"] > start]
        if len(inside) >= MIN_WINDOW_SEGMENTS:
            minutes = max((end - start) / 60.0, 0.25)
            speakers = sorted(
                {i["identified_as"] for i in inside if i["identified_as"]}
            )
            candidates.append(
                {
                    "window_start": round(start, 1),
                    "window_end": round(end, 1),
                    "score": sum(i["score"] for i in inside) / minutes,
                    "n_segments": len(inside),
                    "n_unlabeled": sum(i["unlabeled"] for i in inside),
                    "n_uncertain": sum(i["uncertain"] for i in inside),
                    "n_short": sum(i["short"] for i in inside),
                    "n_overlap": sum(i["overlap"] for i in inside),
                    "speakers": speakers,
                }
            )
        if end >= audio_end:
            break
        start += WINDOW_STEP_SECONDS

    candidates.sort(key=lambda w: w["score"], reverse=True)
    picked: list = []
    for window in candidates:
        if len(picked) >= MAX_WINDOWS_PER_CONVERSATION:
            break
        if any(
            window["window_start"] < p["window_end"]
            and p["window_start"] < window["window_end"]
            for p in picked
        ):
            continue
        picked.append(window)
    return picked


def _window_reasons(window: dict) -> List[str]:
    reasons = []
    if window["n_unlabeled"]:
        reasons.append(f"{window['n_unlabeled']} unlabeled segments")
    if window["n_uncertain"]:
        reasons.append(f"{window['n_uncertain']} segments near the decision threshold")
    if window["n_short"]:
        reasons.append(f"{window['n_short']} short (<2s) segments — the failure regime")
    if window["n_overlap"]:
        reasons.append(f"{window['n_overlap']} cross-speaker overlaps")
    if window.get("prior_annotations"):
        reasons.append(
            f"{window['prior_annotations']} human annotations already in this window"
        )
    return reasons


async def _prior_annotation_points(conversation_ids: List[str]) -> dict:
    """Per conversation, the time points a human has already annotated."""
    points: dict = {cid: [] for cid in conversation_ids}
    async for row in _annotations_collection().find(
        {
            "conversation_id": {"$in": conversation_ids},
            "annotation_type": {"$in": HUMAN_ANNOTATION_TYPES},
            "source": "user",
        },
        {
            "conversation_id": 1,
            "segment_start_time": 1,
            "new_start": 1,
            "insert_start": 1,
        },
    ):
        for field in ("segment_start_time", "new_start", "insert_start"):
            value = row.get(field)
            if value is not None:
                points[row["conversation_id"]].append(float(value))
                break
    return points


def _diverse_batch(windows: list, batch_size: int) -> list:
    """Round-robin across speaker signatures so one pair can't fill the batch."""
    by_signature: dict = {}
    for window in windows:
        signature = "+".join(window["speakers"]) or "(unknown only)"
        by_signature.setdefault(signature, []).append(window)
    for group in by_signature.values():
        group.sort(key=lambda w: w["score"], reverse=True)
    signatures = sorted(
        by_signature, key=lambda s: by_signature[s][0]["score"], reverse=True
    )

    batch: list = []
    while len(batch) < batch_size and any(by_signature.values()):
        for signature in signatures:
            group = by_signature[signature]
            if group:
                batch.append(group.pop(0))
                if len(batch) >= batch_size:
                    break
    return batch


async def suggest_annotation_targets(
    user: User,
    batch_size: int = 6,
    window_seconds: float = 120.0,
):
    """Ranked conversation windows whose ground-truth annotation is expected
    to teach the speaker pipeline the most per minute of effort."""
    batch_size = max(1, min(batch_size, 20))
    window_seconds = max(MIN_WINDOW_SECONDS, min(window_seconds, MAX_WINDOW_SECONDS))
    threshold = get_diarization_settings().get("similarity_threshold", 0.5)

    decided = {
        _target_key(r["conversation_id"], r["window_start"])
        async for r in _targets_collection().find(
            {}, {"conversation_id": 1, "window_start": 1}
        )
    }

    query = {
        "deleted": {"$ne": True},
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    if not user.is_superuser:
        query["user_id"] = str(user.user_id)

    collection = Conversation.get_pymongo_collection()
    windows: list = []
    scanned = 0
    async for doc in collection.find(
        query,
        {
            "conversation_id": 1,
            "title": 1,
            "created_at": 1,
            "client_id": 1,
            "audio_total_duration": 1,
            "active_transcript_version": 1,
            "transcript_versions.version_id": 1,
            "transcript_versions.segments.start": 1,
            "transcript_versions.segments.end": 1,
            "transcript_versions.segments.text": 1,
            "transcript_versions.segments.identified_as": 1,
            "transcript_versions.segments.confidence": 1,
            "transcript_versions.segments.segment_type": 1,
        },
    ):
        scanned += 1
        for window in _conversation_windows(doc, threshold, window_seconds):
            if _target_key(doc["conversation_id"], window["window_start"]) in decided:
                continue
            windows.append(
                {
                    **window,
                    "conversation_id": doc["conversation_id"],
                    "conversation_title": doc.get("title"),
                    "conversation_date": str(doc.get("created_at") or ""),
                    "client_id": doc.get("client_id"),
                }
            )

    prior = await _prior_annotation_points(
        list({w["conversation_id"] for w in windows})
    )
    for window in windows:
        n_prior = sum(
            1
            for t in prior.get(window["conversation_id"], [])
            if window["window_start"] <= t <= window["window_end"]
        )
        window["prior_annotations"] = n_prior
        # A partially annotated window still needs finishing but resolves less
        # new uncertainty per minute than an untouched one.
        window["score"] = round(window["score"] / (1.0 + n_prior), 4)

    windows.sort(key=lambda w: w["score"], reverse=True)
    batch = _diverse_batch(windows, batch_size)
    for window in batch:
        window["reasons"] = _window_reasons(window)

    return {
        "threshold": threshold,
        "window_seconds": window_seconds,
        "batch": batch,
        "conversations_scanned": scanned,
        "pool_windows": len(windows),
        "decided_total": len(decided),
    }


async def decide_annotation_targets(user: User, decisions: List[dict]):
    """Record window outcomes so finished/skipped windows leave the queue."""
    targets = _targets_collection()
    recorded = 0
    for decision in decisions:
        conversation_id = decision.get("conversation_id")
        window_start = decision.get("window_start")
        if not conversation_id or window_start is None:
            continue
        await targets.update_one(
            {
                "conversation_id": conversation_id,
                "window_start": round(float(window_start), 1),
            },
            {
                "$set": {
                    "window_end": decision.get("window_end"),
                    "decision": decision.get("decision"),
                    "decided_by": str(user.user_id),
                    "decided_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        recorded += 1
    return {"recorded": recorded, "status": "ok"}
