"""Authenticated push-device and notification-intent interfaces."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from backend.auth import current_active_user
from backend.models.notification import NotificationDelivery, NotificationIntent
from backend.services.notifications import (
    NotificationCommand,
    NotificationQuotaExceeded,
    enqueue_notification,
    register_push_device,
    unregister_push_device,
)
from backend.users import User

router = APIRouter(prefix="/notifications", tags=["notifications"])


class PushDeviceRegistration(BaseModel):
    expo_push_token: str = Field(min_length=20, max_length=256)
    platform: Literal["ios", "android"]
    app_version: str | None = Field(default=None, max_length=40)
    build_version: str | None = Field(default=None, max_length=40)


def _intent_payload(
    intent: NotificationIntent, deliveries: list[NotificationDelivery] | None = None
) -> dict:
    return {
        "notification_id": intent.notification_id,
        "type": intent.notification_type,
        "title": intent.title,
        "body": intent.body,
        "action": intent.action,
        "route": intent.route,
        "deliver_at": intent.deliver_at,
        "expires_at": intent.expires_at,
        "state": intent.state,
        "source": intent.source,
        "last_error": intent.last_error,
        "created_at": intent.created_at,
        "deliveries": [
            {
                "delivery_id": item.delivery_id,
                "installation_id": item.installation_id,
                "state": item.state,
                "attempts": item.attempts,
                "last_error": item.last_error,
                "submitted_at": item.submitted_at,
                "receipt_checked_at": item.receipt_checked_at,
            }
            for item in deliveries or []
        ],
    }


@router.put("/devices/{installation_id}")
async def put_push_device(
    installation_id: str,
    body: PushDeviceRegistration,
    user: User = Depends(current_active_user),
):
    if not installation_id or len(installation_id) > 100:
        raise HTTPException(status_code=422, detail="invalid installation_id")
    try:
        device = await register_push_device(
            user_id=str(user.id),
            installation_id=installation_id,
            expo_push_token=body.expo_push_token,
            platform=body.platform,
            app_version=body.app_version,
            build_version=body.build_version,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "installation_id": device.installation_id,
        "platform": device.platform,
        "enabled": device.enabled,
        "last_registered_at": device.last_registered_at,
    }


@router.delete("/devices/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_push_device(
    installation_id: str,
    user: User = Depends(current_active_user),
) -> Response:
    await unregister_push_device(user_id=str(user.id), installation_id=installation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_notification(
    body: NotificationCommand,
    user: User = Depends(current_active_user),
):
    try:
        intent, created = await enqueue_notification(
            user_id=str(user.id),
            command=body,
            source="agent",
            actor_id=str(user.id),
        )
    except NotificationQuotaExceeded as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    return {**_intent_payload(intent), "created": created}


@router.get("/{notification_id}")
async def get_notification(
    notification_id: str,
    user: User = Depends(current_active_user),
):
    intent = await NotificationIntent.find_one(
        NotificationIntent.notification_id == notification_id,
        NotificationIntent.user_id == str(user.id),
    )
    if intent is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    deliveries = await NotificationDelivery.find(
        NotificationDelivery.notification_id == notification_id,
        NotificationDelivery.user_id == str(user.id),
    ).to_list()
    return _intent_payload(intent, deliveries)
