"""RQ jobs for screenshots shared from a phone.

Describing a screenshot is a Codex round trip of tens of seconds, so the upload
endpoint enqueues rather than waiting: its contract is that the image is durable when
it returns, not that it has been understood. The ``screenshot_descriptions`` cron is
the backstop for anything this job never got to.
"""

import logging
from typing import Any

from advanced_omi_backend.controllers.queue_controller import (
    JOB_RESULT_TTL,
    memory_queue,
)
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.services.screenshots.describe import describe_screenshot
from advanced_omi_backend.services.vision import CodexVisionUnavailable

logger = logging.getLogger(__name__)

_DESCRIBE_TIMEOUT = 900


@async_job(redis=False, beanie=True, timeout=_DESCRIBE_TIMEOUT)
async def describe_screenshot_job(item_id: str) -> dict[str, Any]:
    """Describe one freshly shared screenshot."""

    try:
        return await describe_screenshot(item_id)
    except CodexVisionUnavailable as exc:
        # Not this screenshot's fault, and describe_screenshot has already released
        # the claim without spending an attempt. The cron backstop retries it.
        logger.warning("Codex unavailable for screenshot %s: %s", item_id, exc)
        return {"status": "deferred", "reason": "codex unavailable"}


def enqueue_screenshot_description(item_id: str) -> None:
    """Queue the description pass, never failing the upload it was called from.

    The screenshot is already durable by this point, so a broken queue must not turn
    a successful share into an error the user sees.
    """
    try:
        memory_queue.enqueue(
            describe_screenshot_job,
            item_id,
            job_timeout=_DESCRIBE_TIMEOUT,
            result_ttl=JOB_RESULT_TTL,
        )
    except Exception as exc:  # noqa: BLE001 - the cron backstop covers every failure
        logger.warning(
            "Could not enqueue description for screenshot %s (%s); "
            "the screenshot_descriptions cron will pick it up",
            item_id,
            exc,
        )
