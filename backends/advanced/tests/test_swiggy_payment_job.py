"""Exercise the registered RQ payment-monitor entry point's async body."""

import time
from types import SimpleNamespace

from fakeredis import aioredis as fake_aioredis

from advanced_omi_backend.integrations.swiggy import payment_job
from advanced_omi_backend.services.interaction_modes import (
    InteractionSession,
    InteractionStore,
)


class _ConfiguredStore:
    def __init__(self, path):
        self.configured = True


class _PaymentClient:
    def __init__(self):
        self.calls = []

    async def call(self, server, tool, **arguments):
        self.calls.append((tool, arguments))
        if tool == "check_payment_status":
            return SimpleNamespace(
                data={
                    "status": "success",
                    "terminal": True,
                    "confirmed": False,
                }
            )
        if tool == "confirm_order":
            return SimpleNamespace(data={"status": "CONFIRMED"})
        raise AssertionError(tool)


class _FailingPaymentClient:
    async def call(self, server, tool, **arguments):
        raise RuntimeError("payment status service unavailable")


async def _seed_payment_session(redis_client):
    now = time.time()
    session = InteractionSession(
        interaction_id="interaction-1",
        mode_id="swiggy_order",
        owner_plugin_id="swiggy_instamart",
        user_id="user-1",
        client_id="device-1",
        audio_session_id="audio-1",
        phase="awaiting_payment",
        plugin_state={"order_id": "order-1", "payment_status": "pending"},
        started_at=now,
        last_activity_at=now,
        idle_timeout_seconds=600,
        max_duration_seconds=1800,
    )
    assert await InteractionStore(redis_client).create(session)


async def test_payment_job_confirms_once_and_ends_the_mode(monkeypatch):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    await _seed_payment_session(redis_client)
    client = _PaymentClient()
    monkeypatch.setattr(payment_job, "FileTokenStore", _ConfiguredStore)
    monkeypatch.setattr(payment_job, "SwiggyClient", lambda store: client)
    spoken = []
    published = []

    async def capture_speech(redis, client_id, session_id, text):
        spoken.append(text)

    async def capture_sse(redis, user_id, event_type, data):
        published.append((event_type, data))

    monkeypatch.setattr(payment_job, "speak_on_device", capture_speech)
    monkeypatch.setattr(payment_job, "publish_sse", capture_sse)

    result = await payment_job.monitor_instamart_payment_job.__wrapped__(
        interaction_id="interaction-1",
        user_id="user-1",
        client_id="device-1",
        audio_session_id="audio-1",
        token_directory="/private/swiggy",
        order_id="order-1",
        paas_id="paas-1",
        polling_interval_ms=5000,
        max_polling_ms=300000,
        redis_client=redis_client,
    )

    assert [name for name, _ in client.calls] == [
        "check_payment_status",
        "confirm_order",
    ]
    assert client.calls[1][1] == {"orderId": "order-1", "paasId": "paas-1"}
    ended = await InteractionStore(redis_client).get("interaction-1")
    assert ended.status == "ended"
    assert ended.end_reason == "payment_success"
    assert result["reason"] == "payment_success"
    assert spoken and published[0][0] == "interaction.ended"


async def test_payment_job_surfaces_monitor_failure_and_ends_uncertain_mode(
    monkeypatch,
):
    redis_client = fake_aioredis.FakeRedis(decode_responses=True)
    await _seed_payment_session(redis_client)
    monkeypatch.setattr(payment_job, "FileTokenStore", _ConfiguredStore)
    monkeypatch.setattr(
        payment_job, "SwiggyClient", lambda store: _FailingPaymentClient()
    )
    spoken = []

    async def capture_speech(redis, client_id, session_id, text):
        spoken.append(text)

    async def ignore_sse(redis, user_id, event_type, data):
        return None

    monkeypatch.setattr(payment_job, "speak_on_device", capture_speech)
    monkeypatch.setattr(payment_job, "publish_sse", ignore_sse)

    result = await payment_job.monitor_instamart_payment_job.__wrapped__(
        interaction_id="interaction-1",
        user_id="user-1",
        client_id="device-1",
        audio_session_id="audio-1",
        token_directory="/private/swiggy",
        order_id="order-1",
        paas_id="paas-1",
        polling_interval_ms=5000,
        max_polling_ms=300000,
        redis_client=redis_client,
    )

    ended = await InteractionStore(redis_client).get("interaction-1")
    assert result["reason"] == "payment_monitor_error"
    assert ended.status == "ended"
    assert ended.end_reason == "payment_monitor_error"
    assert "check the swiggy app" in spoken[0].lower()
