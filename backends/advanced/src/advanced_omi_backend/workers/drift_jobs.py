"""RQ jobs for speaker-label drift analysis support."""

from typing import Any, Dict

from rq import get_current_job

from advanced_omi_backend.controllers.drift_controller import (
    backfill_cluster_embeddings,
)
from advanced_omi_backend.models.job import async_job


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
