import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.heartbeat import (
    FLEET_HEALTH_KEY,
    evaluate_fleet_health,
    is_rq_worker_fresh,
)
from advanced_omi_backend.routers.modules import health_routes
from advanced_omi_backend.services.observability import health_poller
from advanced_omi_backend.workers.orchestrator.health_monitor import HealthMonitor


class FakeAsyncRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.hashes = {}

    async def get(self, key):
        return self.values.get(key)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value


class FakeMongoAdmin:
    async def command(self, _command):
        return {"ok": 1}


class FakeMongoClient:
    admin = FakeMongoAdmin()


class FakeSyncRedis:
    def __init__(self, heartbeat=None):
        self.heartbeat = heartbeat

    def ping(self):
        return True

    def get(self, key):
        assert key == FLEET_HEALTH_KEY
        return self.heartbeat


class FakeFleetRedis:
    def __init__(self):
        self.writes = []

    def set(self, key, value, ex):
        self.writes.append((key, json.loads(value), ex))


class FakeProcessManager:
    def get_status(self):
        return {
            "rq-1": {"is_alive": True},
            "streaming-stt": {"is_alive": True},
            "audio-persistence": {"is_alive": False},
        }


def test_evaluate_worker_fleet_rejects_missing_and_stale_heartbeats():
    missing = evaluate_fleet_health(None, now=1_000)
    assert missing["healthy"] is False
    assert missing["status"] == "missing"

    stale = evaluate_fleet_health(
        json.dumps({"status": "healthy", "timestamp": 900, "workers_total": 12}),
        now=1_000,
        max_age_seconds=30,
    )
    assert stale["healthy"] is False
    assert stale["status"] == "stale"
    assert stale["age_seconds"] == pytest.approx(100)


def test_evaluate_worker_fleet_accepts_fresh_healthy_heartbeat():
    result = evaluate_fleet_health(
        json.dumps({"status": "healthy", "timestamp": 995, "workers_total": 12}),
        now=1_000,
        max_age_seconds=30,
    )
    assert result["healthy"] is True
    assert result["status"] == "healthy"
    assert result["workers_total"] == 12


def test_rq_worker_freshness_rejects_stale_redis_registrations():
    fresh = SimpleNamespace(
        last_heartbeat=datetime.fromtimestamp(990, tz=timezone.utc), worker_ttl=60
    )
    stale = SimpleNamespace(
        last_heartbeat=datetime.fromtimestamp(900, tz=timezone.utc), worker_ttl=60
    )

    assert is_rq_worker_fresh(fresh, now=1_000) is True
    assert is_rq_worker_fresh(stale, now=1_000) is False


def test_health_monitor_publishes_actual_child_process_counts(monkeypatch):
    redis = FakeFleetRedis()
    monitor = HealthMonitor(FakeProcessManager(), SimpleNamespace(), redis)
    monkeypatch.setattr(
        "advanced_omi_backend.workers.orchestrator.health_monitor.time.time",
        lambda: 1_000,
    )

    monitor._publish_fleet_health("unhealthy", "one child is down")

    key, payload, ttl = redis.writes[0]
    assert key == FLEET_HEALTH_KEY
    assert ttl > 30
    assert payload == {
        "status": "unhealthy",
        "timestamp": 1_000,
        "workers_total": 3,
        "workers_alive": 2,
        "detail": "one child is down",
    }


@pytest.mark.asyncio
async def test_worker_fleet_poller_records_outage_once_then_recovery(monkeypatch):
    redis = FakeAsyncRedis()
    recorded = []

    async def capture_event(**event):
        recorded.append(event)

    monkeypatch.setattr(health_poller, "record_event", capture_event)

    await health_poller._poll_worker_fleet(redis, now=1_000)
    await health_poller._poll_worker_fleet(redis, now=1_015)

    assert len(recorded) == 1
    assert recorded[0]["severity"] == "critical"
    assert recorded[0]["category"] == "service"
    assert recorded[0]["source"] == "workers"
    assert recorded[0]["metadata"]["health"] == "missing"

    redis.values[FLEET_HEALTH_KEY] = json.dumps(
        {"status": "healthy", "timestamp": 1_020, "workers_total": 12}
    )
    await health_poller._poll_worker_fleet(redis, now=1_025)

    assert len(recorded) == 2
    assert recorded[1]["severity"] == "info"
    assert "recovered" in recorded[1]["title"].lower()


@pytest.mark.asyncio
async def test_readiness_requires_fresh_worker_fleet_heartbeat(monkeypatch):
    monkeypatch.setattr(health_routes, "mongo_client", FakeMongoClient())
    checks = []

    async def memory_ready():
        checks.append("memory")
        return True

    monkeypatch.setattr(
        health_routes,
        "get_memory_service",
        lambda: SimpleNamespace(test_connection=memory_ready),
    )

    monkeypatch.setattr(health_routes, "redis_conn", FakeSyncRedis())
    unavailable = await health_routes.readiness_check()
    assert unavailable.status_code == 503
    assert json.loads(unavailable.body)["status"] == "not_ready"
    assert checks == []

    heartbeat = json.dumps(
        {"status": "healthy", "timestamp": 1_000, "workers_total": 12}
    )
    monkeypatch.setattr(health_routes.time, "time", lambda: 1_005)
    monkeypatch.setattr(health_routes, "redis_conn", FakeSyncRedis(heartbeat))
    ready = await health_routes.readiness_check()
    assert ready.status_code == 200
    assert json.loads(ready.body)["status"] == "ready"
    assert checks == ["memory"]


@pytest.mark.asyncio
async def test_readiness_rejects_invalid_memory_runtime(monkeypatch):
    heartbeat = json.dumps(
        {"status": "healthy", "timestamp": 1_000, "workers_total": 12}
    )
    monkeypatch.setattr(health_routes.time, "time", lambda: 1_005)
    monkeypatch.setattr(health_routes, "mongo_client", FakeMongoClient())
    monkeypatch.setattr(health_routes, "redis_conn", FakeSyncRedis(heartbeat))

    async def memory_unavailable():
        return False

    monkeypatch.setattr(
        health_routes,
        "get_memory_service",
        lambda: SimpleNamespace(test_connection=memory_unavailable),
    )

    response = await health_routes.readiness_check()

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "not_ready"
