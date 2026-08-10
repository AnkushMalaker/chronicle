import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Imported after making the repository root available to pytest.
from edge import service_manager


def _operation(**overrides):
    operation = {
        "id": "abc123",
        "service": "backend",
        "action": "restart",
        "status": "running",
        "ok": None,
        "log": "",
        "phase": "",
        "started_at": 100.0,
        "finished_at": None,
    }
    operation.update(overrides)
    return operation


def test_report_operation_event_writes_structured_ledger_entry(monkeypatch):
    posted = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        posted.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(service_manager, "TOKEN", "test-token")
    monkeypatch.setattr(service_manager.requests, "post", fake_post)
    monkeypatch.setattr(service_manager.platform, "node", lambda: "test-node")

    service_manager._report_operation_event(
        _operation(status="done", ok=True, log="restarted cleanly", finished_at=104.0)
    )

    assert posted["url"].endswith("/api/admin/system-events/ingest")
    assert posted["headers"] == {"Authorization": "Bearer test-token"}
    assert posted["json"] == {
        "severity": "info",
        "category": "service",
        "source": "service-manager:test-node",
        "title": "Restart backend done (abc123)",
        "detail": "restarted cleanly",
        "metadata": {
            "event_type": "service_operation",
            "operation_id": "abc123",
            "node": "test-node",
            "service": "backend",
            "action": "restart",
            "status": "done",
            "ok": True,
            "started_at": 100.0,
            "finished_at": 104.0,
            "phase": "",
        },
    }


def test_run_operation_reports_completion(monkeypatch):
    reported = []
    completed = threading.Event()
    operation = _operation()

    def capture(op):
        reported.append(dict(op))
        completed.set()

    monkeypatch.setattr(service_manager, "_report_operation_event", capture)
    assert service_manager._busy_lock.acquire(blocking=False)

    service_manager._run_operation(operation, lambda _op: True)

    assert completed.wait(timeout=2)
    assert reported[0]["status"] == "done"
    assert reported[0]["ok"] is True
    assert reported[0]["finished_at"] is not None
