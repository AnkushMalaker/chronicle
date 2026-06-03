"""
Data-cleaning RQ jobs.

Provides:
- ``analyze_silence_batch_job``: decode each conversation's audio, compute
  loudness metrics, and cache them on the conversation so the Data Cleaning UI
  can filter/sort without re-decoding.
- ``auto_clean_job``: system-wide sweep that analyzes unanalyzed audio and
  auto-archives conversations meeting the configured silence "level". Enqueued
  by the ``auto_clean`` cron job (off by default) so the work is visible in the
  queue/jobs pages.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.controllers.conversation_controller import (
    archive_conversation_audio_doc,
)
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.utils.silence_analysis import analyze_conversation_audio

logger = logging.getLogger(__name__)

# Conservative fallback level used when config is missing (matches defaults.yml).
_DEFAULT_AUTO_CLEAN = {
    "silence_threshold_dbfs": -50.0,
    "min_silent_fraction": 0.97,
    "min_duration": 30.0,
    "max_archive_per_run": 100,
}


async def _analyze_and_store(
    conv: Conversation,
) -> Optional["Conversation.SilenceAnalysis"]:
    """Compute silence analysis for a conversation and cache it. Returns the
    analysis, or None on failure (logged, never raised)."""
    try:
        result = await analyze_conversation_audio(conv.conversation_id)
        result["analyzed_at"] = datetime.now(timezone.utc)
        sa = Conversation.SilenceAnalysis(**result)
        conv.silence_analysis = sa
        await conv.save()
        return sa
    except Exception as e:
        logger.warning(f"Silence analysis failed for {conv.conversation_id[:12]}: {e}")
        return None


@async_job(redis=False, beanie=True)
async def analyze_silence_batch_job(
    user_id: str,
    conversation_ids: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Compute and cache silence analysis for a user's conversations.

    Args:
        user_id: Owner whose conversations are analyzed (scopes the query).
        conversation_ids: Optional explicit subset; if omitted, all of the
            user's non-deleted, non-archived conversations with audio.
        force: Re-analyze even if a cached result already exists.

    Returns a summary dict (never raises for a single conversation failure).
    """
    start = time.time()

    if conversation_ids:
        query = Conversation.find(
            {"conversation_id": {"$in": conversation_ids}, "user_id": user_id}
        )
    else:
        query = Conversation.find(
            {
                "user_id": user_id,
                "deleted": {"$ne": True},
                "audio_archived": {"$ne": True},
                "audio_chunks_count": {"$gt": 0},
            }
        )

    conversations = await query.to_list()

    analyzed = 0
    skipped = 0
    failed = 0

    for conv in conversations:
        if conv.audio_archived or not conv.audio_chunks_count:
            skipped += 1
            continue
        if conv.silence_analysis is not None and not force:
            skipped += 1
            continue

        if await _analyze_and_store(conv) is not None:
            analyzed += 1
        else:
            failed += 1

    summary = {
        "success": True,
        "analyzed": analyzed,
        "skipped": skipped,
        "failed": failed,
        "total": len(conversations),
        "processing_time_seconds": round(time.time() - start, 2),
    }
    logger.info(f"🧹 analyze_silence_batch_job done: {summary}")
    return summary


@async_job(redis=False, beanie=True, timeout=3600)
async def auto_clean_job(dry_run: bool = False) -> Dict[str, Any]:
    """System-wide auto-clean sweep.

    Analyzes any unanalyzed audio, then archives conversations whose silent
    fraction (at the configured threshold) and duration meet the configured
    "level" (``data_cleaning.auto_clean`` in config). Archiving hard-deletes
    audio bytes (a metadata stub is kept), so the run is bounded by
    ``max_archive_per_run`` as a safety valve.

    Args:
        dry_run: When True, count would-archive conversations without deleting.

    Returns a summary dict (never raises for a single-conversation failure).
    """
    start = time.time()

    cfg = get_service_config("data_cleaning")
    ac = cfg.get("auto_clean", {}) or {}
    threshold = float(
        ac.get("silence_threshold_dbfs", _DEFAULT_AUTO_CLEAN["silence_threshold_dbfs"])
    )
    min_silent = float(
        ac.get("min_silent_fraction", _DEFAULT_AUTO_CLEAN["min_silent_fraction"])
    )
    min_dur = float(ac.get("min_duration", _DEFAULT_AUTO_CLEAN["min_duration"]))
    max_per_run = int(
        ac.get("max_archive_per_run", _DEFAULT_AUTO_CLEAN["max_archive_per_run"])
    )

    conversations = await Conversation.find(
        {
            "deleted": {"$ne": True},
            "audio_archived": {"$ne": True},
            "audio_chunks_count": {"$gt": 0},
        }
    ).to_list()

    analyzed = 0
    matched = 0
    archived = 0
    failed = 0
    cap_hit = False

    for conv in conversations:
        if archived >= max_per_run:
            cap_hit = True
            break

        sa = conv.silence_analysis
        if sa is None:
            sa = await _analyze_and_store(conv)
            if sa is None:
                failed += 1
                continue
            analyzed += 1

        duration = conv.audio_total_duration or 0.0
        if duration < min_dur:
            continue
        if sa.silent_fraction(threshold) < min_silent:
            continue

        matched += 1
        if dry_run:
            continue
        try:
            await archive_conversation_audio_doc(conv, reason="near_silent")
            archived += 1
        except Exception as e:
            failed += 1
            logger.warning(
                f"Auto-clean archive failed for {conv.conversation_id[:12]}: {e}"
            )

    summary = {
        "success": True,
        "dry_run": dry_run,
        "scanned": len(conversations),
        "analyzed": analyzed,
        "matched": matched,
        "archived": archived,
        "failed": failed,
        "cap_hit": cap_hit,
        "level": {
            "silence_threshold_dbfs": threshold,
            "min_silent_fraction": min_silent,
            "min_duration": min_dur,
            "max_archive_per_run": max_per_run,
        },
        "processing_time_seconds": round(time.time() - start, 2),
    }
    if cap_hit:
        logger.warning(
            f"🧹 auto_clean_job hit max_archive_per_run={max_per_run}; "
            f"{matched - archived}+ candidates remain for the next run"
        )
    logger.info(f"🧹 auto_clean_job done: {summary}")
    return summary
