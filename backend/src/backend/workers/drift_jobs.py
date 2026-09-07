"""RQ jobs for speaker-label drift analysis support."""

from typing import Any, Dict

from rq import get_current_job

from backend.controllers.drift_controller import (
    backfill_cluster_embeddings,
    drift_fingerprint,
    find_drift_conversations,
    store_drift_report,
)
from backend.models.job import async_job


@async_job(redis=False, beanie=True, timeout=3600)
async def drift_scan_job() -> Dict[str, Any]:
    """Scan all conversations for speaker-label drift, reporting progress as it goes.

    The route serves a cached report when nothing relevant changed; this job only
    runs on a cache miss (or forced re-analyze) and refreshes the cache when done.
    """
    job = get_current_job()

    def publish_progress(processed: int, total: int, drifted: int) -> None:
        if not job:
            return
        job.meta["batch_progress"] = {
            "percent": round(100 * processed / total) if total else 100,
            "message": f"Scanning {processed}/{total} · {drifted} drifted so far",
            "done": processed,
            "total": total,
        }
        job.save_meta()

    # Fingerprint BEFORE scanning: if the world changes mid-scan, the stored
    # fingerprint won't match it and the next open recomputes instead of serving
    # a report that missed the change.
    fingerprint = await drift_fingerprint()
    report = await find_drift_conversations(progress_callback=publish_progress)
    await store_drift_report(report, fingerprint)
    return report


@async_job(redis=False, beanie=True, timeout=7200)
async def cluster_embedding_backfill_job() -> Dict[str, Any]:
    """Populate missing per-cluster embeddings used by the drift report."""
    job = get_current_job()

    def publish_progress(
        processed: int,
        total: int,
        backfilled: int,
        skipped: int,
        failed: int,
    ) -> None:
        if not job:
            return
        job.meta["batch_progress"] = {
            "percent": round(100 * processed / total) if total else 100,
            "message": (
                f"Scanning {processed}/{total} · {backfilled} backfilled"
                f" · {skipped} skipped · {failed} failed"
            ),
            "done": processed,
            "total": total,
        }
        job.save_meta()

    return await backfill_cluster_embeddings(
        only_missing=True,
        progress_callback=publish_progress,
    )
