"""
Data-audit RQ jobs.

Provides:
- ``analyze_audio_batch_job``: run VAD over each conversation's audio, write
  frame scores to the chunk documents, and cache a probability histogram on
  the conversation so the Data Audit UI can filter/sort without re-decoding.
- ``auto_clean_job``: system-wide sweep that analyzes unanalyzed audio and
  auto-archives conversations meeting the configured speech-free "level".
  Enqueued by the ``auto_clean`` cron job (off by default) so the work is
  visible in the queue/jobs pages.
- ``export_annotation_dataset_job``: build an annotation dataset zip —
  speech-region WAV clips + transcript manifest — for selected conversations.
"""

import json
import logging
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from beanie import PydanticObjectId
from rq import get_current_job

from advanced_omi_backend.config_loader import get_service_config
from advanced_omi_backend.controllers.conversation_controller import (
    archive_conversation_audio_doc,
)
from advanced_omi_backend.llm_client import async_generate
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.observability.system_events import record_event_sync
from advanced_omi_backend.users import User
from advanced_omi_backend.utils.annotation_export import (
    MANIFEST_NAME,
    META_NAME,
    ZIP_NAME,
    active_segments,
    build_clip_record,
    export_dir,
)
from advanced_omi_backend.utils.audio_chunk_utils import (
    audio_cache_duration_matches,
    reconstruct_audio_segment,
)
from advanced_omi_backend.utils.sensitivity_screening import (
    DEFAULT_SENSITIVITY_POLICY,
    build_screening_prompt,
    parse_flagged,
    screenable_segments,
)
from advanced_omi_backend.utils.transcript_slicing import slice_segments
from advanced_omi_backend.utils.vad_analysis import (
    analyze_conversation_audio,
    frame_speech_intervals,
    merge_speech_regions,
    subtract_intervals,
)

logger = logging.getLogger(__name__)

# Conservative fallback level used when config is missing (matches defaults.yml).
_DEFAULT_AUTO_CLEAN = {
    "speech_prob_threshold": 0.5,
    "max_speech_fraction": 0.03,
    "min_duration": 30.0,
    "max_archive_per_run": 100,
}


def get_sensitivity_policy() -> str:
    """Default privacy-screen policy from config (``data_audit.export.sensitivity``)."""
    cfg = get_service_config("data_audit") or {}
    sens = (cfg.get("export") or {}).get("sensitivity") or {}
    policy = (sens.get("policy") or "").strip()
    return policy or DEFAULT_SENSITIVITY_POLICY


async def _analyze_and_store(
    conv: Conversation,
) -> Optional["Conversation.VadAnalysis"]:
    """Run VAD analysis for a conversation and cache the summary. Returns the
    analysis, or None on failure (logged, never raised)."""
    try:
        result = await analyze_conversation_audio(conv.conversation_id)
        result["analyzed_at"] = datetime.now(timezone.utc)
        va = Conversation.VadAnalysis(**result)
        conv.vad_analysis = va
        # The fresh VAD reflects the actual chunk set. If its implied duration
        # disagrees with the stored audio_total_duration, the conversation's
        # audio metadata is internally inconsistent (reconnect-duplication: a
        # bad recording). Flag it once and surface it on the System Errors page;
        # the data-audit list then excludes it so it stops perpetually showing
        # as "needs analysis" (re-analysis can't reconcile corrupt metadata).
        va_dur = (va.frame_count or 0) * (va.frame_hop_ms or 0) / 1000.0
        stored_dur = conv.audio_total_duration or 0.0
        if (
            not audio_cache_duration_matches(va_dur, stored_dur)
            and not conv.audio_integrity_error
        ):
            reason = (
                f"audio duration drift: stored {stored_dur:.0f}s / "
                f"{conv.audio_chunks_count} chunks vs {va_dur:.0f}s from actual audio"
            )
            conv.audio_integrity_error = reason
            record_event_sync(
                severity="error",
                category="data_integrity",
                source="analyze_audio_batch_job",
                title=f"Corrupt audio metadata: {conv.conversation_id[:8]} (duration drift)",
                detail=reason,
                conversation_id=conv.conversation_id,
                client_id=conv.client_id,
                user_id=conv.user_id,
                metadata={
                    "stored_duration_s": round(stored_dur, 1),
                    "stored_chunks": conv.audio_chunks_count,
                    "vad_duration_s": round(va_dur, 1),
                },
            )
            logger.warning(
                f"Flagged corrupt audio metadata for {conv.conversation_id[:12]}: {reason}"
            )
        await conv.save()
        return va
    except Exception as e:
        logger.warning(f"VAD analysis failed for {conv.conversation_id[:12]}: {e}")
        return None


@async_job(redis=False, beanie=True)
async def analyze_audio_batch_job(
    user_id: str,
    conversation_ids: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Compute and cache VAD analysis for a user's conversations.

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
                # Skip conversations already flagged as corrupt (surfaced on the
                # System Errors page); an explicit conversation_ids list overrides.
                "audio_integrity_error": None,
            }
        )

    conversations = await query.to_list()

    # One up-front pass: anything already analyzed (or with no audio to
    # analyze) is skipped immediately, so progress is reported over only the
    # conversations that actually need work — "2/5 (178 already analyzed)"
    # rather than ticking 0/183 through cached entries.
    def _needs_analysis(conv: Conversation) -> bool:
        if conv.audio_archived or not conv.audio_chunks_count:
            return False
        if force or conv.vad_analysis is None:
            return True
        # Re-analyze a cached summary that no longer matches the current audio
        # (chunk set changed in place since it was computed).
        va = conv.vad_analysis
        cached = (va.frame_count or 0) * (va.frame_hop_ms or 0) / 1000.0
        return not audio_cache_duration_matches(
            cached, conv.audio_total_duration or 0.0
        )

    pending = [c for c in conversations if _needs_analysis(c)]
    skipped = len(conversations) - len(pending)

    job = get_current_job()
    total = len(pending)

    def _progress(done: int) -> None:
        """Publish per-conversation progress to job.meta for the UI to poll."""
        if not job:
            return
        job.meta["batch_progress"] = {
            "percent": round(100 * done / total) if total else 100,
            "message": f"Analyzing {done}/{total} ({skipped} already analyzed, skipped)",
            "done": done,
            "total": total,
        }
        job.save_meta()

    analyzed = 0
    failed = 0

    _progress(0)
    for done, conv in enumerate(pending, start=1):
        if await _analyze_and_store(conv) is not None:
            analyzed += 1
        else:
            failed += 1
        _progress(done)

    summary = {
        "success": True,
        "analyzed": analyzed,
        "skipped": skipped,
        "failed": failed,
        "total": len(conversations),
        "processing_time_seconds": round(time.time() - start, 2),
    }
    logger.info(f"🧹 analyze_audio_batch_job done: {summary}")
    return summary


@async_job(redis=False, beanie=True, timeout=3600)
async def auto_clean_job(dry_run: bool = False) -> Dict[str, Any]:
    """System-wide auto-clean sweep.

    Analyzes any unanalyzed audio, then archives conversations whose speech
    fraction (at the configured VAD probability threshold) and duration meet
    the configured "level" (``data_audit.auto_clean`` in config). Archiving
    hard-deletes audio bytes (a metadata stub is kept), so the run is bounded
    by ``max_archive_per_run`` as a safety valve.

    Args:
        dry_run: When True, count would-archive conversations without deleting.

    Returns a summary dict (never raises for a single-conversation failure).
    """
    start = time.time()

    cfg = get_service_config("data_audit")
    ac = cfg.get("auto_clean", {}) or {}
    threshold = float(
        ac.get("speech_prob_threshold", _DEFAULT_AUTO_CLEAN["speech_prob_threshold"])
    )
    max_speech = float(
        ac.get("max_speech_fraction", _DEFAULT_AUTO_CLEAN["max_speech_fraction"])
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

        va = conv.vad_analysis
        if va is None:
            va = await _analyze_and_store(conv)
            if va is None:
                failed += 1
                continue
            analyzed += 1

        duration = conv.audio_total_duration or 0.0
        if duration < min_dur:
            continue
        if va.speech_fraction(threshold) > max_speech:
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
            "speech_prob_threshold": threshold,
            "max_speech_fraction": max_speech,
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


async def _screen_one_conversation(conv: Conversation, policy: str) -> Dict[str, Any]:
    """Run the privacy screen over one conversation's active transcript.

    Returns a report with the flagged segments (time range + quote + category +
    reason). Never raises for a single-conversation LLM failure — records an
    ``error`` instead so one bad conversation doesn't sink the batch.
    """
    segments = active_segments(conv)
    pairs = screenable_segments(segments)
    report: Dict[str, Any] = {
        "conversation_id": conv.conversation_id,
        "title": conv.title,
        "client_id": conv.client_id,
        "segment_count": len(pairs),
        "flagged": [],
    }
    if not pairs:
        return report

    prompt = build_screening_prompt(policy, pairs)
    try:
        raw = await async_generate(prompt, operation="sensitivity_screening")
        flagged = parse_flagged(raw, {index for index, _ in pairs})
    except Exception as e:
        logger.warning(
            f"Sensitivity screen failed for {conv.conversation_id[:12]}: {e}"
        )
        report["error"] = str(e)
        return report

    seg_by_index = {index: seg for index, seg in pairs}
    flagged_seconds = 0.0
    for item in flagged:
        seg = seg_by_index[item["index"]]
        item["start"] = round(seg.start, 2)
        item["end"] = round(seg.end, 2)
        item["speaker"] = seg.identified_as or seg.speaker
        item["text"] = seg.text
        flagged_seconds += max(0.0, seg.end - seg.start)
    report["flagged"] = flagged
    report["flagged_seconds"] = round(flagged_seconds, 2)
    return report


@async_job(redis=False, beanie=True, timeout=1800)
async def screen_conversations_job(
    user_id: str,
    conversation_ids: List[str],
    policy: Optional[str] = None,
) -> Dict[str, Any]:
    """Privacy-screen selected conversations against a shareability policy.

    For each conversation, an LLM applies ``policy`` (or the configured
    default) to the active transcript and flags segments too personal to send
    to an outside annotator. Returns per-conversation flagged segments with
    their time ranges; the caller reviews them and passes the confirmed ranges
    back to the export job as ``excluded_ranges``. This job only screens —
    nothing is exported or mutated here.
    """
    start = time.time()
    policy = (policy or "").strip() or get_sensitivity_policy()

    user = await User.get(PydanticObjectId(user_id))
    is_super = bool(user and user.is_superuser)

    job = get_current_job()
    ids = list(dict.fromkeys(conversation_ids))
    total = len(ids)

    def _progress(done: int, label: str) -> None:
        """Publish per-conversation progress to job.meta for the UI to poll."""
        if not job:
            return
        job.meta["batch_progress"] = {
            "percent": round(100 * done / total) if total else 100,
            "message": f"Screened {done}/{total}" + (f" · {label}" if label else ""),
            "done": done,
            "total": total,
            "flagged_so_far": flagged_total,
        }
        job.save_meta()

    reports: List[Dict[str, Any]] = []
    flagged_total = 0
    _progress(0, "starting…")
    for done, cid in enumerate(ids):
        conv = await Conversation.find_one(Conversation.conversation_id == cid)
        if not conv:
            reports.append({"conversation_id": cid, "skipped_reason": "not found"})
            _progress(done + 1, "")
            continue
        if not is_super and conv.user_id != user_id:
            reports.append(
                {"conversation_id": cid, "skipped_reason": "access forbidden"}
            )
            _progress(done + 1, "")
            continue
        report = await _screen_one_conversation(conv, policy)
        flagged_total += len(report.get("flagged") or [])
        reports.append(report)
        _progress(done + 1, conv.title or cid[:8])

    summary = {
        "success": True,
        "policy": policy,
        "conversations": reports,
        "totals": {
            "conversation_count": len(reports),
            "flagged_segments": flagged_total,
        },
        "processing_time_seconds": round(time.time() - start, 2),
    }
    logger.info(
        f"🔏 screen_conversations_job done: {flagged_total} flagged segments "
        f"across {len(reports)} conversations ({summary['processing_time_seconds']}s)"
    )
    return summary


async def _collect_raw_intervals(
    conversation_id: str, threshold: float
) -> Tuple[Optional[List[List[float]]], float, int]:
    """Raw speech intervals from cached chunk frame scores (streaming cursor).

    Returns (intervals, last_chunk_end_seconds, sample_rate); intervals is
    None when any chunk lacks VAD scores (caller should analyze first).
    """
    collection = AudioChunkDocument.get_pymongo_collection()
    cursor = collection.find(
        {"conversation_id": conversation_id},
        {
            "start_time": 1,
            "end_time": 1,
            "sample_rate": 1,
            "vad.scores": 1,
            "vad.frame_hop_ms": 1,
        },
    ).sort("chunk_index", 1)

    intervals: List[List[float]] = []
    last_end = 0.0
    sample_rate = 16000
    first = True
    async for chunk in cursor:
        if first:
            sample_rate = int(chunk.get("sample_rate") or 16000)
            first = False
        vad = chunk.get("vad")
        if not vad or vad.get("scores") is None:
            return None, 0.0, sample_rate
        intervals.extend(
            frame_speech_intervals(
                vad["scores"],
                float(vad["frame_hop_ms"]) / 1000.0,
                float(chunk["start_time"]),
                threshold=threshold,
            )
        )
        last_end = float(chunk["end_time"])
    return intervals, last_end, sample_rate


async def _export_conversation_clips(
    zf: zipfile.ZipFile,
    conv: Conversation,
    mode: str,
    pad_seconds: float,
    speech_threshold: float,
    merge_gap_seconds: float,
    excluded_ranges: Optional[List[List[float]]] = None,
) -> Tuple[List[dict], float, float]:
    """Write the conversation's WAV clip(s) into the zip; return its manifest
    records, total clipped seconds, and excluded (withheld) seconds.

    Mode ``clips``: one padded WAV per VAD speech region (silence cropped).
    Mode ``full``: a single untouched WAV spanning the whole conversation —
    no VAD needed.

    ``excluded_ranges`` (absolute conversation seconds, from the privacy
    screen) are carved out of the regions so the withheld audio + its
    transcript never enter a clip.
    """
    cid = conv.conversation_id

    if mode == "full":
        duration = conv.audio_total_duration or 0.0
        if duration <= 0:
            raise ValueError("Conversation has no audio duration")
        regions = [[0.0, duration]]
        first = await AudioChunkDocument.find_one(
            AudioChunkDocument.conversation_id == cid
        )
        sample_rate = first.sample_rate if first else 16000
    else:
        intervals, last_end, sample_rate = await _collect_raw_intervals(
            cid, speech_threshold
        )
        if intervals is None:
            # Unanalyzed audio — run VAD inline (idempotent), then retry.
            if await _analyze_and_store(conv) is None:
                raise ValueError("VAD analysis failed")
            intervals, last_end, sample_rate = await _collect_raw_intervals(
                cid, speech_threshold
            )
            if intervals is None:
                raise ValueError("VAD scores missing after analysis")

        duration = conv.audio_total_duration or last_end
        # Cached speech_regions are built with the default 0.3s pad — always
        # re-merge here so the export honors the requested padding.
        regions = merge_speech_regions(
            intervals,
            duration,
            pad_seconds=pad_seconds,
            merge_gap_seconds=merge_gap_seconds,
        )

    # Carve out privacy-screened ranges so withheld audio/transcript is
    # never written. Done after padding/merge so padding can't re-expose a cut.
    kept_seconds = sum(t1 - t0 for t0, t1 in regions)
    if excluded_ranges:
        regions = subtract_intervals(regions, excluded_ranges)
    excluded_seconds = kept_seconds - sum(t1 - t0 for t0, t1 in regions)

    segments = active_segments(conv)
    created_at = conv.created_at.isoformat() if conv.created_at else None

    records: List[dict] = []
    clip_seconds = 0.0
    for i, (t0, t1) in enumerate(regions):
        wav = await reconstruct_audio_segment(cid, t0, t1)
        record = build_clip_record(
            conversation_id=cid,
            conversation_title=conv.title,
            client_id=conv.client_id,
            conversation_created_at=created_at,
            clip_index=i,
            region_start=t0,
            region_end=t1,
            sample_rate=sample_rate,
            segments=slice_segments(segments, t0, t1),
        )
        zf.writestr(record["audio_path"], wav)
        records.append(record)
        clip_seconds += t1 - t0
    return records, clip_seconds, round(max(0.0, excluded_seconds), 2)


@async_job(redis=False, beanie=True, timeout=3600)
async def export_annotation_dataset_job(
    user_id: str,
    export_id: str,
    conversation_ids: List[str],
    mode: str = "clips",
    pad_seconds: float = 1.0,
    speech_threshold: float = 0.5,
    merge_gap_seconds: float = 3.0,
    excluded_ranges: Optional[Dict[str, List[List[float]]]] = None,
    sensitivity_policy: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an annotation dataset zip for the selected conversations.

    Mode ``clips`` (default): derive speech regions from chunk VAD scores
    (running VAD inline for unanalyzed audio) with the requested padding and
    cut one sample-accurate WAV clip per region. Mode ``full``: export each
    conversation as a single untouched WAV. Either way each clip is paired
    with the sliced active transcript in ``manifest.jsonl``; the zip and an
    ``export.json`` summary land in ``DATA_DIR/exports/{export_id}/`` for
    the download endpoint.

    ``excluded_ranges`` maps ``conversation_id`` → withheld time ranges (from
    the privacy screen); those ranges are carved out of each conversation's
    audio and transcript. ``sensitivity_policy`` is recorded in the metadata
    for auditability.

    Per-conversation failures are recorded as ``skipped_reason``; the job
    only raises on export-level failures (e.g. disk errors).
    """
    start = time.time()
    excluded_ranges = excluded_ranges or {}
    user = await User.get(PydanticObjectId(user_id))
    is_super = bool(user and user.is_superuser)

    out_dir = export_dir(export_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / ZIP_NAME

    conv_summaries: List[dict] = []
    manifest_records: List[dict] = []
    total_clip_seconds = 0.0
    total_excluded_seconds = 0.0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for cid in dict.fromkeys(conversation_ids):
            summary: Dict[str, Any] = {
                "conversation_id": cid,
                "title": None,
                "client_id": None,
            }
            conv = await Conversation.find_one(Conversation.conversation_id == cid)
            if conv:
                summary["title"] = conv.title
                summary["client_id"] = conv.client_id

            if not conv:
                summary["skipped_reason"] = "not found"
            elif not is_super and conv.user_id != user_id:
                summary["skipped_reason"] = "access forbidden"
            elif conv.deleted:
                summary["skipped_reason"] = "deleted"
            elif conv.audio_archived:
                summary["skipped_reason"] = "audio archived"
            elif not conv.audio_chunks_count:
                summary["skipped_reason"] = "no audio"
            else:
                try:
                    records, clip_seconds, excluded_seconds = (
                        await _export_conversation_clips(
                            zf,
                            conv,
                            mode,
                            pad_seconds,
                            speech_threshold,
                            merge_gap_seconds,
                            excluded_ranges.get(cid),
                        )
                    )
                    manifest_records.extend(records)
                    total_clip_seconds += clip_seconds
                    total_excluded_seconds += excluded_seconds
                    summary["clip_count"] = len(records)
                    summary["clip_seconds"] = round(clip_seconds, 2)
                    if excluded_seconds > 0:
                        summary["excluded_seconds"] = excluded_seconds
                except Exception as e:
                    logger.exception(f"Export failed for conversation {cid[:12]}")
                    summary["skipped_reason"] = f"error: {e}"
            conv_summaries.append(summary)

        exported = [s for s in conv_summaries if "skipped_reason" not in s]
        meta = {
            "export_id": export_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": user_id,
            "params": {
                "mode": mode,
                "pad_seconds": pad_seconds,
                "speech_threshold": speech_threshold,
                "merge_gap_seconds": merge_gap_seconds,
                "screened": bool(excluded_ranges),
                "sensitivity_policy": sensitivity_policy if excluded_ranges else None,
            },
            "conversations": conv_summaries,
            "totals": {
                "conversation_count": len(conv_summaries),
                "exported_conversations": len(exported),
                "clip_count": len(manifest_records),
                "total_clip_seconds": round(total_clip_seconds, 2),
                "excluded_seconds": round(total_excluded_seconds, 2),
            },
        }
        zf.writestr(
            MANIFEST_NAME,
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in manifest_records),
        )
        zf.writestr(META_NAME, json.dumps(meta, indent=2, ensure_ascii=False))

    meta["totals"]["zip_bytes"] = zip_path.stat().st_size
    (out_dir / META_NAME).write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    logger.info(
        f"📦 export_annotation_dataset_job {export_id}: "
        f"{meta['totals']['clip_count']} clips / "
        f"{meta['totals']['total_clip_seconds']:.0f}s from "
        f"{meta['totals']['exported_conversations']}/{len(conv_summaries)} "
        f"conversations ({time.time() - start:.1f}s)"
    )
    return {"success": True, **meta}
