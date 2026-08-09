"""RQ jobs for asynchronous manual-memory enrichment."""

import logging
from typing import Any

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    memory_queue,
)
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.manual_memories.image import analyze_image
from advanced_omi_backend.services.vision import CodexVisionUnavailable

logger = logging.getLogger(__name__)
_TIMEOUT = 900


@async_job(redis=False, beanie=True, timeout=_TIMEOUT)
async def analyze_manual_memory_image_job(
    memory_id: str, attachment_id: str
) -> dict[str, Any]:
    try:
        return await analyze_image(memory_id, attachment_id)
    except CodexVisionUnavailable as exc:
        logger.warning("Image enrichment unavailable for %s: %s", memory_id, exc)
        return {"status": "deferred"}


def enqueue_manual_memory_image(memory_id: str, attachment_id: str) -> None:
    try:
        memory_queue.enqueue(
            analyze_manual_memory_image_job,
            memory_id,
            attachment_id,
            job_timeout=_TIMEOUT,
            result_ttl=JOB_RESULT_TTL,
        )
    except Exception as exc:  # noqa: BLE001 - cron is the durable backstop
        logger.warning("Could not enqueue manual memory %s: %s", memory_id, exc)
