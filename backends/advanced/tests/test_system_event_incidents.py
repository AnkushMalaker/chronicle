"""Regression tests for durable system-event incidents."""

from datetime import datetime

import pytest

from advanced_omi_backend.services.observability import system_events


class FakeSystemEvent:
    documents = []

    def __init__(self, **values):
        self.__dict__.update(values)
        self.resolved_at = values.get("resolved_at")
        self.occurrences = values.get("occurrences", 1)

    @classmethod
    async def find_one(cls, query):
        for document in cls.documents:
            if all(
                getattr(document, key, None) == value for key, value in query.items()
            ):
                return document
        return None

    async def insert(self):
        self.documents.append(self)

    async def save(self):
        return None


def _event(*, title, incident_key=None, resolves_incident=False):
    return {
        "severity": "info" if resolves_incident else "warning",
        "category": "plugin",
        "source": "hermes",
        "title": title,
        "detail": "ConnectTimeout",
        "traceback": None,
        "user_id": None,
        "client_id": None,
        "conversation_id": None,
        "metadata": {"plugin_id": "hermes"},
        "fingerprint": title,
        "incident_key": incident_key,
        "resolves_incident": resolves_incident,
    }


@pytest.mark.asyncio
async def test_plugin_outage_is_one_incident_until_real_recovery(monkeypatch):
    FakeSystemEvent.documents = []
    monkeypatch.setattr(system_events, "SystemEvent", FakeSystemEvent)
    outage = _event(
        title="Plugin 'hermes' degraded: dependency unreachable",
        incident_key="plugin-dependency:hermes",
    )

    first = await system_events._upsert(outage)
    duplicate = await system_events._upsert(outage)

    assert first is FakeSystemEvent.documents[0]
    assert duplicate is None
    assert len(FakeSystemEvent.documents) == 1
    assert first.occurrences == 2
    assert len(first.occurrence_times) == 2
    assert all(
        isinstance(occurred_at, datetime) for occurred_at in first.occurrence_times
    )
    assert isinstance(first.last_seen_at, datetime)
    assert first.resolved_at is None

    recovery = _event(
        title="Plugin 'hermes' recovered",
        incident_key="plugin-dependency:hermes",
        resolves_incident=True,
    )
    recovered = await system_events._upsert(recovery)
    duplicate_recovery = await system_events._upsert(recovery)

    assert recovered is FakeSystemEvent.documents[1]
    assert duplicate_recovery is None
    assert len(FakeSystemEvent.documents) == 2
    assert first.resolved_at is not None
