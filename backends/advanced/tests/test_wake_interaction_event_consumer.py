import json
from unittest.mock import AsyncMock, Mock

import pytest

from advanced_omi_backend.services.wakeword.interaction_event_consumer import (
    WakeInteractionEventConsumer,
)


@pytest.mark.asyncio
async def test_response_lifecycle_event_becomes_an_immutable_wake_fact():
    ledger = Mock(append=AsyncMock())
    consumer = WakeInteractionEventConsumer(AsyncMock(), ledger)
    payload = {
        "wake_trace_id": "7ce4d46b-232f-47f9-8148-d595ed344cf2",
        "stage": "response_done",
        "occurred_at": 1_770_000_003.0,
        "user_id": "user-1",
        "client_id": "device-1",
        "audio_session_id": "audio-1",
        "capture_epoch": 4,
        "voice_session_id": "voice-1",
        "turn_id": "turn-1",
        "turn_revision": 0,
        "response_id": "response-1",
        "generation": 8,
        "response_state": "done",
    }

    await consumer._handle({b"event": json.dumps(payload).encode()})

    fact = ledger.append.await_args.args[0]
    assert (fact.stage, fact.ordinal) == ("response_done", 9)
    assert fact.wake_trace_id == payload["wake_trace_id"]
    assert fact.response_id == "response-1"
    assert fact.occurred_at.timestamp() == payload["occurred_at"]


@pytest.mark.asyncio
async def test_command_stage_event_uses_the_same_immutable_fact_interface():
    ledger = Mock(append=AsyncMock())
    consumer = WakeInteractionEventConsumer(AsyncMock(), ledger)
    payload = {
        "wake_trace_id": "7ce4d46b-232f-47f9-8148-d595ed344cf2",
        "stage": "dispatched",
        "occurred_at": 1_770_000_002.5,
        "user_id": "user-1",
        "client_id": "device-1",
        "audio_session_id": "audio-1",
        "capture_epoch": 4,
        "voice_session_id": "voice-1",
        "turn_id": "turn-1",
        "turn_revision": 0,
        "wakeword": "hey_hermes",
        "payload": {
            "dispatch_ms": 4250.0,
            "plugins": [{"plugin_id": "hermes", "duration_ms": 4200.0}],
        },
    }

    await consumer._handle({"event": json.dumps(payload)})

    fact = ledger.append.await_args.args[0]
    assert (fact.stage, fact.ordinal) == ("dispatched", 3)
    assert fact.wakeword == "hey_hermes"
    assert fact.response_id is None
    assert fact.payload == payload["payload"]


@pytest.mark.asyncio
async def test_unknown_lifecycle_stage_is_rejected_before_ledger_write():
    ledger = Mock(append=AsyncMock())
    consumer = WakeInteractionEventConsumer(AsyncMock(), ledger)

    with pytest.raises(ValueError, match="unsupported"):
        await consumer._handle({b"event": json.dumps({"stage": "invented"}).encode()})

    ledger.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_reclaims_and_acks_stale_lifecycle_fact():
    payload = {
        "wake_trace_id": "7ce4d46b-232f-47f9-8148-d595ed344cf2",
        "stage": "response_done",
        "occurred_at": 1_770_000_003.0,
        "user_id": "user-1",
        "client_id": "device-1",
        "audio_session_id": "audio-1",
        "capture_epoch": 4,
        "voice_session_id": "voice-1",
        "turn_id": "turn-1",
        "turn_revision": 0,
        "response_id": "response-1",
        "generation": 8,
        "response_state": "done",
    }
    redis_client = AsyncMock()
    redis_client.xautoclaim.return_value = (
        b"0-0",
        [(b"9-0", {b"event": json.dumps(payload).encode()})],
        [],
    )
    consumer = WakeInteractionEventConsumer(redis_client, Mock(append=AsyncMock()))

    await consumer._recover_pending_once()

    redis_client.xack.assert_awaited_once()
