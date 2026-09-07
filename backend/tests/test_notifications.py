import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import ValidationError

from backend.controllers import queue_controller
from backend.models.notification import (
    NotificationDelivery,
    NotificationIntent,
    PushDevice,
    utcnow,
)
from backend.services import notifications
from backend.services.notifications import NotificationCommand, PushTicket
from backend.workers import notification_jobs


@pytest.fixture
async def notification_db(mongo_service, monkeypatch):
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_notifications_db"]
    await init_beanie(
        database=database,
        document_models=[PushDevice, NotificationIntent, NotificationDelivery],
    )
    for model in (PushDevice, NotificationIntent, NotificationDelivery):
        await model.delete_all()

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(notifications, "distributed_lock", unlocked)
    yield
    await client.drop_database("test_notifications_db")
    client.close()


class FakeProvider:
    def __init__(self, *, receipt=None, missing_receipts=False):
        self.sent = []
        self.receipt = receipt or {"status": "ok"}
        self.missing_receipts = missing_receipts

    async def send(self, messages):
        self.sent.extend(messages)
        return [
            PushTicket(token=item.token, status="ok", ticket_id=f"ticket-{index}")
            for index, item in enumerate(messages)
        ]

    async def receipts(self, ticket_ids):
        if self.missing_receipts:
            return {}
        return {ticket_id: self.receipt for ticket_id in ticket_ids}


def command(**updates):
    values = {
        "notification_type": "agent",
        "title": "Reminder",
        "body": "Check Chronicle",
    }
    values.update(updates)
    return NotificationCommand(**values)


def test_notification_actions_are_allowlisted():
    with pytest.raises(ValidationError):
        command(action="open_chronicle_route", route="https://example.com")
    with pytest.raises(ValidationError):
        command(action="none", route="timeline")
    assert command(action="open_immich").action == "open_immich"


def test_receipt_enqueue_replaces_ended_scheduled_retry(monkeypatch):
    deleted = []
    existing = type(
        "EndedJob",
        (),
        {
            "id": "notification-receipts",
            "ended_at": utcnow(),
            "delete": lambda self: deleted.append(True),
        },
    )()
    monkeypatch.setattr(
        queue_controller.Job,
        "fetch",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        queue_controller, "get_job_status_from_rq", lambda _job: "scheduled"
    )
    enqueued = []

    def enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))
        return type("Job", (), {"id": kwargs["job_id"]})()

    monkeypatch.setattr(queue_controller.default_queue, "enqueue", enqueue)

    job_id = queue_controller.enqueue_notification_receipts()

    assert job_id == "notification-receipts"
    assert deleted == [True]
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_reused_token_is_atomically_transferred_to_new_owner(notification_db):
    token = "ExpoPushToken[abcdefghijklmnopqrstuvwxyz]"
    await notifications.register_push_device(
        user_id="old-user",
        installation_id="old-installation",
        expo_push_token=token,
        platform="ios",
    )
    await notifications.register_push_device(
        user_id="new-user",
        installation_id="new-installation",
        expo_push_token=token,
        platform="ios",
    )

    devices = await PushDevice.find_all().to_list()
    assert [(item.user_id, item.installation_id) for item in devices] == [
        ("new-user", "new-installation")
    ]


@pytest.mark.asyncio
async def test_conflicting_destination_is_preserved_during_token_transfer(
    notification_db,
):
    reused_token = "ExpoPushToken[abcdefghijklmnopqrstuvwxyz]"
    destination_token = "ExpoPushToken[zyxwvutsrqponmlkjihgfedcba]"
    await notifications.register_push_device(
        user_id="old-user",
        installation_id="old-installation",
        expo_push_token=reused_token,
        platform="ios",
    )
    await notifications.register_push_device(
        user_id="new-user",
        installation_id="new-installation",
        expo_push_token=destination_token,
        platform="ios",
    )

    with pytest.raises(ValueError, match="different push token"):
        await notifications.register_push_device(
            user_id="new-user",
            installation_id="new-installation",
            expo_push_token=reused_token,
            platform="ios",
        )

    devices = await PushDevice.find_all().sort("installation_id").to_list()
    assert [item.expo_push_token for item in devices] == [
        destination_token,
        reused_token,
    ]


@pytest.mark.asyncio
async def test_dispatch_records_ticket_then_provider_acceptance(
    notification_db, monkeypatch
):
    token = "ExpoPushToken[abcdefghijklmnopqrstuvwxyz]"
    await notifications.register_push_device(
        user_id="user",
        installation_id="installation",
        expo_push_token=token,
        platform="android",
    )
    monkeypatch.setattr(notifications, "_queue_dispatch", lambda _id: "job-one")
    intent, _ = await notifications.enqueue_notification(
        user_id="user",
        command=command(action="open_immich", dedupe_key="backup-day"),
        source="agent",
        actor_id="user",
    )
    provider = FakeProvider()

    submitted = await notifications.dispatch_notification(
        intent.notification_id, provider
    )
    delivery = await NotificationDelivery.find_one({})
    assert submitted["state"] == "submitted"
    assert delivery.provider_ticket_id == "ticket-0"
    assert provider.sent[0].data["action"] == "open_immich"
    assert 0 < provider.sent[0].ttl_seconds <= 24 * 60 * 60

    delivery.receipt_due_at = utcnow() - timedelta(seconds=1)
    await delivery.save()
    receipts = await notifications.check_due_receipts(provider)
    stored = await NotificationIntent.find_one(
        NotificationIntent.notification_id == intent.notification_id
    )
    assert receipts["accepted"] == 1
    assert stored.state == "provider_accepted"


@pytest.mark.asyncio
async def test_missing_receipt_stops_polling_after_intent_expiry(
    notification_db, monkeypatch
):
    token = "ExpoPushToken[abcdefghijklmnopqrstuvwxyz]"
    await notifications.register_push_device(
        user_id="user",
        installation_id="installation",
        expo_push_token=token,
        platform="ios",
    )
    monkeypatch.setattr(notifications, "_queue_dispatch", lambda _id: "job-one")
    intent, _ = await notifications.enqueue_notification(
        user_id="user",
        command=command(expires_at=utcnow() + timedelta(minutes=1)),
        source="agent",
        actor_id="user",
    )
    provider = FakeProvider(missing_receipts=True)
    await notifications.dispatch_notification(intent.notification_id, provider)
    delivery = await NotificationDelivery.find_one({})
    delivery.receipt_due_at = utcnow() - timedelta(seconds=1)
    await delivery.save()
    intent.expires_at = utcnow() - timedelta(seconds=1)
    await intent.save()

    result = await notifications.check_due_receipts(provider)

    await delivery.sync()
    await intent.sync()
    assert result["failed"] == 1
    assert delivery.state == "failed"
    assert intent.state == "failed"


@pytest.mark.asyncio
async def test_device_not_registered_receipt_disables_token(
    notification_db, monkeypatch
):
    token = "ExpoPushToken[abcdefghijklmnopqrstuvwxyz]"
    await notifications.register_push_device(
        user_id="user",
        installation_id="installation",
        expo_push_token=token,
        platform="ios",
    )
    monkeypatch.setattr(notifications, "_queue_dispatch", lambda _id: "job-one")
    intent, _ = await notifications.enqueue_notification(
        user_id="user", command=command(), source="agent", actor_id="user"
    )
    await notifications.dispatch_notification(intent.notification_id, FakeProvider())
    delivery = await NotificationDelivery.find_one({})
    delivery.receipt_due_at = utcnow() - timedelta(seconds=1)
    await delivery.save()

    provider = FakeProvider(
        receipt={"status": "error", "details": {"error": "DeviceNotRegistered"}}
    )
    await notifications.check_due_receipts(provider)
    device = await PushDevice.find_one({})
    assert device.enabled is False
    assert device.disabled_reason == "DeviceNotRegistered"


@pytest.mark.asyncio
async def test_scheduled_intent_waits_and_expired_intent_never_queues(
    notification_db, monkeypatch
):
    queued = []
    monkeypatch.setattr(
        notifications, "_queue_dispatch", lambda item: queued.append(item) or "job"
    )
    future = utcnow() + timedelta(hours=1)
    intent, _ = await notifications.enqueue_notification(
        user_id="user",
        command=command(deliver_at=future, expires_at=future + timedelta(hours=1)),
        source="agent",
        actor_id="user",
    )
    expired = NotificationIntent(
        user_id="user",
        notification_type="agent",
        title="Old",
        body="Old",
        expires_at=utcnow() - timedelta(seconds=1),
        source="agent",
    )
    await expired.insert()

    result = await notifications.queue_due_notifications()

    assert result == {"due": 0, "queued": 0, "expired": 1}
    assert queued == []
    assert (await NotificationIntent.get(expired.id)).state == "expired"
    assert (await NotificationIntent.get(intent.id)).state == "pending"


@pytest.mark.asyncio
async def test_agent_quota_and_dedupe_are_owner_scoped(notification_db, monkeypatch):
    monkeypatch.setattr(notifications, "_queue_dispatch", lambda item: f"job-{item}")
    first, created = await notifications.enqueue_notification(
        user_id="user",
        command=command(dedupe_key="same-reminder"),
        source="agent",
        actor_id="user",
    )
    duplicate, duplicate_created = await notifications.enqueue_notification(
        user_id="user",
        command=command(dedupe_key="same-reminder"),
        source="agent",
        actor_id="user",
    )
    assert created is True
    assert duplicate_created is False
    assert duplicate.notification_id == first.notification_id

    for index in range(notifications.AGENT_HOURLY_LIMIT - 1):
        await notifications.enqueue_notification(
            user_id="user",
            command=command(dedupe_key=f"reminder-{index}"),
            source="agent",
            actor_id="user",
        )
    with pytest.raises(notifications.NotificationQuotaExceeded):
        await notifications.enqueue_notification(
            user_id="user",
            command=command(dedupe_key="over-quota"),
            source="agent",
            actor_id="user",
        )


@pytest.mark.asyncio
async def test_concurrent_dedupe_is_single_flight(notification_db, monkeypatch):
    lock = asyncio.Lock()

    @asynccontextmanager
    async def local_lock(*_args, **_kwargs):
        async with lock:
            yield

    monkeypatch.setattr(notifications, "distributed_lock", local_lock)
    monkeypatch.setattr(notifications, "_queue_dispatch", lambda _id: "job-one")

    results = await asyncio.gather(
        *(
            notifications.enqueue_notification(
                user_id="user",
                command=command(dedupe_key="same-concurrent-reminder"),
                source="agent",
                actor_id="user",
            )
            for _ in range(2)
        )
    )

    assert [created for _intent, created in results] == [True, False]
    assert results[0][0].notification_id == results[1][0].notification_id


@pytest.mark.asyncio
async def test_real_dispatch_worker_entrypoint_uses_service(
    notification_db, monkeypatch
):
    monkeypatch.setattr(
        notification_jobs,
        "dispatch_notification",
        lambda notification_id: _async_value(
            {"notification_id": notification_id, "state": "submitted"}
        ),
    )

    result = await notification_jobs.dispatch_notification_job.__wrapped__("notice-one")

    assert result == {"notification_id": "notice-one", "state": "submitted"}


@pytest.mark.asyncio
async def test_real_receipt_cron_enqueues_due_work(notification_db, monkeypatch):
    delivery = NotificationDelivery(
        notification_id="notice-one",
        user_id="user",
        installation_id="installation",
        expo_push_token="ExpoPushToken[abcdefghijklmnopqrstuvwxyz]",
        state="submitted",
        provider_ticket_id="ticket-one",
        receipt_due_at=utcnow() - timedelta(seconds=1),
    )
    await delivery.insert()
    monkeypatch.setattr(
        queue_controller,
        "enqueue_notification_receipts",
        lambda: "notification-receipts",
    )

    result = await notifications.queue_receipt_check()

    assert result == {"due": 1, "job_id": "notification-receipts"}


@pytest.mark.asyncio
async def test_real_receipt_worker_entrypoint_uses_service(
    notification_db, monkeypatch
):
    monkeypatch.setattr(
        notification_jobs,
        "check_due_receipts",
        lambda: _async_value({"checked": 1, "accepted": 1, "failed": 0}),
    )

    result = await notification_jobs.check_notification_receipts_job.__wrapped__()

    assert result == {"checked": 1, "accepted": 1, "failed": 0}


@pytest.mark.asyncio
async def test_real_dispatch_worker_records_provider_failure(
    notification_db, monkeypatch
):
    intent = NotificationIntent(
        user_id="user",
        notification_type="priority",
        title="Backup",
        body="Open Immich",
        expires_at=utcnow() + timedelta(hours=1),
        source="timeline_immich_gate",
    )
    await intent.insert()

    async def fail(_notification_id):
        raise notifications.TransientPushError("Expo returned 503")

    monkeypatch.setattr(notification_jobs, "dispatch_notification", fail)
    with pytest.raises(notifications.TransientPushError):
        await notification_jobs.dispatch_notification_job.__wrapped__(
            intent.notification_id
        )

    stored = await NotificationIntent.get(intent.id)
    assert stored.state == "failed"
    assert "503" in (stored.last_error or "")


async def _async_value(value):
    return value
