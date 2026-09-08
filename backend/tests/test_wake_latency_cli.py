import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.services.wakeword import benchmark

TRACE_A = "7ce4d46b-232f-47f9-8148-d595ed344cf2"
TRACE_B = "46bc4b51-9b24-40fd-a73b-00523242b428"
START = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)


def _document(trace_id, stage, ordinal, milliseconds, *, payload=None):
    return {
        "_id": f"mongo-{trace_id}-{ordinal}",
        "wake_trace_id": trace_id,
        "stage": stage,
        "ordinal": ordinal,
        "occurred_at": START + timedelta(milliseconds=milliseconds),
        "user_id": "user-1",
        "client_id": "phone-1",
        "audio_session_id": "audio-1",
        "capture_epoch": 3,
        "payload": payload or {},
    }


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sort_spec = None
        self.requested_length = None

    def sort(self, spec):
        self.sort_spec = spec
        return self

    async def to_list(self, *, length):
        self.requested_length = length
        return self.rows


class _Collection:
    def __init__(self, aggregate_rows, documents_by_trace):
        self.aggregate_rows = aggregate_rows
        self.documents_by_trace = documents_by_trace
        self.pipeline = None
        self.find_queries = []
        self.cursors = []

    def aggregate(self, pipeline):
        self.pipeline = pipeline

        async def rows():
            for row in self.aggregate_rows:
                yield row

        return rows()

    def find(self, query):
        self.find_queries.append(query)
        cursor = _Cursor(self.documents_by_trace.get(query["wake_trace_id"], []))
        self.cursors.append(cursor)
        return cursor


class _Client:
    def __init__(self, collection):
        self.collection = collection
        self.database_name = None
        self.closed = False

    def __getitem__(self, database_name):
        self.database_name = database_name
        return {benchmark.COLLECTION: self.collection}

    def close(self):
        self.closed = True


def _args(**overrides):
    values = {"trace_id": None, "client_id": None, "limit": 5}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_run_filters_latest_traces_by_client_and_closes_client(monkeypatch):
    documents = [
        _document(TRACE_A, "armed", 0, 0),
        _document(TRACE_A, "end_of_turn", 1, 250),
    ]
    collection = _Collection([{"_id": TRACE_A}], {TRACE_A: documents})
    client = _Client(collection)
    monkeypatch.setattr(benchmark, "AsyncIOMotorClient", lambda *args, **kwargs: client)
    monkeypatch.setenv("MONGODB_DATABASE", "wake_cli_test")

    reports = await benchmark._run(_args(client_id="phone-1", limit=2))

    assert client.database_name == "wake_cli_test"
    assert client.closed is True
    assert collection.pipeline == [
        {"$match": {"client_id": "phone-1"}},
        {"$group": {"_id": "$wake_trace_id", "latest": {"$max": "$occurred_at"}}},
        {"$sort": {"latest": -1}},
        {"$limit": 2},
    ]
    assert collection.find_queries == [{"wake_trace_id": TRACE_A}]
    assert collection.cursors[0].sort_spec == [("occurred_at", 1), ("ordinal", 1)]
    assert collection.cursors[0].requested_length is None
    assert reports == [
        {
            "wake_trace_id": TRACE_A,
            "status": "incomplete",
            "observed_stages": ("armed", "end_of_turn"),
            "missing_stages": (
                "command_resolved",
                "dispatched",
                "response_queued",
                "response_ready",
                "response_offered",
                "response_playing",
                "response_done",
            ),
            "metrics_ms": {"wake_capture": 250.0},
            "plugins": (),
        }
    ]


@pytest.mark.asyncio
async def test_run_uses_requested_trace_and_ignores_empty_trace_results(monkeypatch):
    collection = _Collection([], {TRACE_B: []})
    client = _Client(collection)
    monkeypatch.setattr(benchmark, "AsyncIOMotorClient", lambda *args, **kwargs: client)

    reports = await benchmark._run(_args(trace_id=TRACE_B, client_id="other-client"))

    assert collection.pipeline is None
    assert collection.find_queries == [{"wake_trace_id": TRACE_B}]
    assert reports == []
    assert client.closed is True


def test_main_serializes_actual_run_output_from_fake_mongo(monkeypatch, capsys):
    documents = [
        _document(TRACE_A, "armed", 0, 0),
        _document(TRACE_A, "end_of_turn", 1, 500),
    ]
    collection = _Collection([], {TRACE_A: documents})
    client = _Client(collection)
    monkeypatch.setattr(benchmark, "AsyncIOMotorClient", lambda *args, **kwargs: client)
    monkeypatch.setattr("sys.argv", ["wake-latency", "--trace-id", TRACE_A])

    benchmark.main()

    assert json.loads(capsys.readouterr().out) == [
        {
            "wake_trace_id": TRACE_A,
            "status": "incomplete",
            "observed_stages": ["armed", "end_of_turn"],
            "missing_stages": [
                "command_resolved",
                "dispatched",
                "response_queued",
                "response_ready",
                "response_offered",
                "response_playing",
                "response_done",
            ],
            "metrics_ms": {"wake_capture": 500.0},
            "plugins": [],
        }
    ]
    assert client.closed is True


def test_main_rejects_non_positive_limit_before_opening_mongo(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wake-latency", "--limit", "0"])
    with patch.object(benchmark, "AsyncIOMotorClient") as client, pytest.raises(
        SystemExit, match="2"
    ):
        benchmark.main()
    client.assert_not_called()
