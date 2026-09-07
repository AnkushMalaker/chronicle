"""Full-corpus embedding index for iterative background cluster review."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from pymongo import UpdateOne
from rq import get_current_job

from backend.models.conversation import Conversation
from backend.models.job import async_job
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.utils.audio_chunk_utils import reconstruct_audio_segment

MIN_SPEECH_SECONDS = 1.0
MAX_SPEECH_SECONDS = 15.0
MIN_GAP_SECONDS = 2.0
MAX_GAP_SECONDS = 8.0
EMBED_CONCURRENCY = 4
SERVICE_READY_ATTEMPTS = 6
SERVICE_READY_DELAY_SECONDS = 2
PROGRESS_INTERVAL = 100
BULK_WRITE_SIZE = 500

logger = logging.getLogger(__name__)


def _active_segments(doc: dict) -> list[dict]:
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    active = next(
        (version for version in versions if version.get("version_id") == active_id),
        versions[-1] if versions else {},
    )
    return active.get("segments") or []


def _gap_windows(segments: list[dict], duration: float) -> list[tuple[float, float]]:
    occupied = sorted(
        (
            max(0.0, float(segment.get("start", 0))),
            min(duration, float(segment.get("end", 0))),
        )
        for segment in segments
        if float(segment.get("end", 0)) > float(segment.get("start", 0))
    )
    merged: list[tuple[float, float]] = []
    for start, end in occupied:
        if merged and start <= merged[-1][1] + 0.1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in [*merged, (duration, duration)]:
        while start - cursor >= MIN_GAP_SECONDS:
            window_end = min(start, cursor + MAX_GAP_SECONDS)
            windows.append((round(cursor, 3), round(window_end, 3)))
            cursor = window_end
        cursor = max(cursor, end)
    return windows


async def _corpus_candidates(requested_by: str) -> list[dict]:
    query = {
        "user_id": requested_by,
        "deleted": {"$ne": True},
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    projection = {
        "conversation_id": 1,
        "title": 1,
        "created_at": 1,
        "audio_total_duration": 1,
        "active_transcript_version": 1,
        "transcript_versions": 1,
    }
    candidates: list[dict] = []
    async for doc in Conversation.get_pymongo_collection().find(query, projection):
        conversation_id = doc["conversation_id"]
        duration = float(doc.get("audio_total_duration") or 0.0)
        segments = _active_segments(doc)
        for segment_index, segment in enumerate(segments):
            if segment.get("segment_type", "speech") != "speech":
                continue
            start = float(segment.get("start", 0))
            end = min(
                float(segment.get("end", 0)), start + MAX_SPEECH_SECONDS, duration
            )
            if end - start < MIN_SPEECH_SECONDS:
                continue
            candidates.append(
                {
                    "clip_key": f"{conversation_id}:{start:.3f}:{end:.3f}:speech",
                    "conversation_id": conversation_id,
                    "conversation_title": doc.get("title") or conversation_id[:8],
                    "conversation_date": doc.get("created_at"),
                    "segment_index": segment_index,
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "text": (segment.get("text") or "")[:300],
                    "candidate_type": "background_speech",
                    "current_label": segment.get("identified_as")
                    or segment.get("speaker"),
                    "stored_confidence": segment.get("confidence"),
                }
            )
        for start, end in _gap_windows(segments, duration):
            candidates.append(
                {
                    "clip_key": f"{conversation_id}:{start:.3f}:{end:.3f}:noise",
                    "conversation_id": conversation_id,
                    "conversation_title": doc.get("title") or conversation_id[:8],
                    "conversation_date": doc.get("created_at"),
                    "segment_index": -1,
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "text": "",
                    "candidate_type": "noise",
                    "current_label": None,
                    "stored_confidence": None,
                }
            )
    return candidates


def _progress(current: int, total: int, message: str) -> None:
    job = get_current_job()
    if not job:
        return
    job.meta["batch_progress"] = {
        "current": current,
        "done": current,
        "total": total,
        "percent": round(current * 100 / total) if total else 100,
        "message": message,
    }
    job.save_meta()


async def _wait_for_embedding_model(client: SpeakerRecognitionClient) -> str:
    """Tolerate the speaker service restarting while a corpus job is queued."""
    last_info: dict = {}
    for attempt in range(SERVICE_READY_ATTEMPTS):
        last_info = await client.get_embedding_info()
        model = last_info.get("embedding_model")
        if model:
            return model
        if attempt < SERVICE_READY_ATTEMPTS - 1:
            _progress(0, 0, "Waiting for speaker recognition…")
            await asyncio.sleep(SERVICE_READY_DELAY_SECONDS)
    raise RuntimeError(f"Could not resolve speaker embedding model: {last_info}")


@async_job(redis=False, beanie=True, timeout=14400)
async def index_background_corpus_job(requested_by: str, source_revision: str) -> dict:
    started_at = time.perf_counter()
    timings: dict[str, float] = {}
    database = Conversation.get_pymongo_collection().database
    cache = database["background_corpus_embeddings"]
    await cache.create_index(
        [("requested_by", 1), ("clip_key", 1), ("embedding_model", 1)], unique=True
    )
    client = SpeakerRecognitionClient()
    model = await _wait_for_embedding_model(client)

    phase_started = time.perf_counter()
    candidates = await _corpus_candidates(requested_by)
    timings["enumerate_corpus_s"] = time.perf_counter() - phase_started
    job = get_current_job()
    run_id = job.id if job else datetime.now(timezone.utc).isoformat()
    candidate_keys = [candidate["clip_key"] for candidate in candidates]
    phase_started = time.perf_counter()
    cached_ids = {
        row["clip_key"]: row["_id"]
        async for row in cache.find(
            {
                "requested_by": requested_by,
                "clip_key": {"$in": candidate_keys},
                "embedding_model": model,
                "embedding": {"$exists": True, "$ne": None},
            },
            {"clip_key": 1},
        )
    }
    timings["load_cache_s"] = time.perf_counter() - phase_started
    cached_candidates = [
        candidate for candidate in candidates if candidate["clip_key"] in cached_ids
    ]
    missing_candidates = [
        candidate for candidate in candidates if candidate["clip_key"] not in cached_ids
    ]
    semaphore = asyncio.Semaphore(EMBED_CONCURRENCY)
    progress_lock = asyncio.Lock()
    completed = len(cached_candidates)
    cached_count = len(cached_candidates)
    embedded_count = 0
    failures = 0

    phase_started = time.perf_counter()
    for offset in range(0, len(cached_candidates), BULK_WRITE_SIZE):
        batch = cached_candidates[offset : offset + BULK_WRITE_SIZE]
        if batch:
            await cache.bulk_write(
                [
                    UpdateOne(
                        {"_id": cached_ids[candidate["clip_key"]]},
                        {"$set": {"run_id": run_id, **candidate}},
                    )
                    for candidate in batch
                ],
                ordered=False,
            )
    timings["refresh_cached_metadata_s"] = time.perf_counter() - phase_started
    _progress(
        completed,
        len(candidates),
        f"Loaded {completed}/{len(candidates)} cached background vectors",
    )

    async def index(candidate: dict) -> None:
        nonlocal completed, embedded_count, failures
        async with semaphore:
            try:
                wav = await reconstruct_audio_segment(
                    candidate["conversation_id"],
                    candidate["start"],
                    candidate["end"],
                )
                result = await client.extract_speaker_embedding(wav)
                if result.get("error") or not result.get("embedding"):
                    raise RuntimeError(str(result))
                await cache.update_one(
                    {
                        "requested_by": requested_by,
                        "clip_key": candidate["clip_key"],
                        "embedding_model": model,
                    },
                    {
                        "$set": {
                            **candidate,
                            **result,
                            "requested_by": requested_by,
                            "run_id": run_id,
                            "indexed_at": datetime.now(timezone.utc),
                        }
                    },
                    upsert=True,
                )
                embedded_count += 1
            except Exception:
                failures += 1
        async with progress_lock:
            completed += 1
            if completed == len(candidates) or completed % PROGRESS_INTERVAL == 0:
                _progress(
                    completed,
                    len(candidates),
                    f"Embedding new background audio {completed}/{len(candidates)}",
                )

    phase_started = time.perf_counter()
    await asyncio.gather(*(index(candidate) for candidate in missing_candidates))
    timings["embed_missing_s"] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    await cache.delete_many(
        {
            "requested_by": requested_by,
            "embedding_model": model,
            "run_id": {"$ne": run_id},
        }
    )
    counts = {
        kind: await cache.count_documents(
            {
                "requested_by": requested_by,
                "embedding_model": model,
                "candidate_type": kind,
            }
        )
        for kind in ("noise", "background_speech")
    }
    await database["background_cluster_cache"].delete_many(
        {"requested_by": requested_by}
    )
    await database["background_index_runs"].update_one(
        {"requested_by": requested_by},
        {
            "$set": {
                "source_revision": source_revision,
                "indexed_at": datetime.now(timezone.utc),
            }
        },
    )
    timings["finalize_s"] = time.perf_counter() - phase_started
    timings["total_s"] = time.perf_counter() - started_at
    timings = {name: round(seconds, 3) for name, seconds in timings.items()}
    logger.info(
        "Background corpus index timings (%d vectors, %d cached): %s",
        len(candidates),
        cached_count,
        timings,
    )
    return {
        "embedding_model": model,
        "candidates": counts,
        "total": sum(counts.values()),
        "cached": cached_count,
        "embedded": embedded_count,
        "failures": failures,
        "timings": timings,
    }
