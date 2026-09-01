"""RQ entrypoints for the durable notification outbox."""

from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.notification import NotificationIntent, utcnow
from advanced_omi_backend.services.notifications import (
    check_due_receipts,
    dispatch_notification,
)


@async_job(redis=True, beanie=True)
async def dispatch_notification_job(notification_id: str, *, redis_client=None) -> dict:
    try:
        return await dispatch_notification(notification_id)
    except Exception as error:
        intent = await NotificationIntent.find_one(
            NotificationIntent.notification_id == notification_id
        )
        if intent is not None:
            intent.state = "failed"
            intent.last_error = f"{type(error).__name__}: {error}"
            intent.updated_at = utcnow()
            await intent.save()
        raise


@async_job(redis=True, beanie=True)
async def check_notification_receipts_job(*, redis_client=None) -> dict:
    return await check_due_receipts()
