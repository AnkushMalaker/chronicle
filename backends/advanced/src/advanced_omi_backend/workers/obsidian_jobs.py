"""
RQ job definitions for Obsidian ingestion.
"""

import logging
import os

from rq import get_current_job
from rq.job import Job

from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.obsidian_service import obsidian_service

logger = logging.getLogger(__name__)


def count_markdown_files(vault_path: str) -> int:
    """Recursively count markdown files in a vault."""
    count = 0
    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for filename in files:
            if filename.endswith(".md"):
                count += 1
    return count


@async_job(redis=True, beanie=False)
async def ingest_obsidian_vault_job(job_id: str, vault_path: str, redis_client=None) -> dict: # type: ignore
    """
    Long-running ingestion job enqueued on the default RQ queue.
    """
    job = get_current_job()
    logger.info("Starting Obsidian ingestion job %s", job.id)

    # Initialize job meta
    job.meta["status"] = "processing"
    job.meta["processed"] = 0
    job.meta["total_files"] = 0
    job.meta["errors"] = []
    job.meta["vault_path"] = vault_path
    job.save_meta()

    try:
        obsidian_service.setup_database()
    except Exception as exc:
        logger.exception("Database setup failed for job %s: %s", job.id, exc)
        job.meta["status"] = "failed"
        job.meta["error"] = f"Database setup failed: {exc}"
        job.save_meta()
        raise

    if not os.path.exists(vault_path):
        msg = f"Vault path not found: {vault_path}"
        logger.error(msg)
        job.meta["status"] = "failed"
        job.meta["error"] = msg
        job.save_meta()
        return {"status": "failed", "error": msg}

    total = count_markdown_files(vault_path)
    job.meta["total_files"] = total
    job.save_meta()

    processed = 0
    errors = []

    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for filename in files:
            if not filename.endswith(".md"):
                continue

            # Check for cancellation
            job.refresh()
            if job.get_status() == "canceled":
                logger.info("Obsidian ingestion job %s cancelled by user", job.id)
                job.meta["status"] = "cancelled"
                job.save_meta()
                return {"status": "cancelled"}

            try:
                note_data = obsidian_service.parse_obsidian_note(root, filename, vault_path)
                chunks = await obsidian_service.chunking_and_embedding(note_data)
                if chunks:
                    obsidian_service.ingest_note_and_chunks(note_data, chunks)
                
                processed += 1
                job.meta["processed"] = processed
                job.meta["last_file"] = os.path.join(root, filename)
                job.save_meta()
                
            except Exception as exc:
                logger.error("Processing %s failed: %s", filename, exc)
                errors.append(f"{filename}: {exc}")
                job.meta["errors"] = errors
                job.save_meta()

    job.meta["status"] = "completed"
    job.save_meta()
    
    return {
        "status": "completed", 
        "processed": processed, 
        "total": total, 
        "errors": errors
    }
