from types import SimpleNamespace

import pytest

from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.services import plugin_service

pytestmark = pytest.mark.unit


class _Field:
    def __eq__(self, other):
        return other


class _FakeDeferredEvent:
    idempotency_key = _Field()
    rows = {}

    def __init__(self, **values):
        self.__dict__.update(values)

    @classmethod
    async def find_one(cls, key):
        return cls.rows.get(key)

    async def insert(self):
        type(self).rows[self.idempotency_key] = self


async def test_space_terminal_event_is_durable_and_idempotent(monkeypatch):
    _FakeDeferredEvent.rows = {}
    monkeypatch.setattr(plugin_service, "DeferredSpaceEvent", _FakeDeferredEvent)
    monkeypatch.setattr(plugin_service, "dispatch_plugin_event", pytest.fail)
    values = dict(
        event=PluginEvent.MEMORY_PROCESSED,
        user_id="user-1",
        memory_space_id="9f3523c8-af75-469d-995a-7179531f3fc8",
        source_kind="conversation",
        source_id="conversation-1",
        data={"memory_count": 2},
    )

    await plugin_service.dispatch_or_defer_space_event(**values)
    await plugin_service.dispatch_or_defer_space_event(**values)

    assert len(_FakeDeferredEvent.rows) == 1
    event = next(iter(_FakeDeferredEvent.rows.values()))
    assert event.causal_order == 30
    assert event.metadata["idempotency_key"] == event.idempotency_key


async def test_space_streaming_event_is_suppressed_without_an_outbox_row(monkeypatch):
    _FakeDeferredEvent.rows = {}
    monkeypatch.setattr(plugin_service, "DeferredSpaceEvent", _FakeDeferredEvent)
    monkeypatch.setattr(plugin_service, "dispatch_plugin_event", pytest.fail)

    await plugin_service.dispatch_or_defer_space_event(
        event=PluginEvent.TRANSCRIPT_STREAMING,
        user_id="user-1",
        memory_space_id="9f3523c8-af75-469d-995a-7179531f3fc8",
        source_kind="conversation",
        source_id="conversation-1",
        data={"transcript": "private"},
    )

    assert _FakeDeferredEvent.rows == {}


async def test_main_event_dispatches_immediately(monkeypatch):
    calls = []

    async def dispatch(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(success=True)]

    monkeypatch.setattr(plugin_service, "dispatch_plugin_event", dispatch)

    await plugin_service.dispatch_or_defer_space_event(
        event=PluginEvent.CONVERSATION_COMPLETE,
        user_id="user-1",
        memory_space_id=None,
        source_kind="conversation",
        source_id="conversation-1",
        data={},
    )

    assert calls[0]["event"] is PluginEvent.CONVERSATION_COMPLETE
