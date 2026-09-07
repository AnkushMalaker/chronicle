"""Privacy and failure-isolation contracts for memory observability."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.observability import otel_setup
from backend.services.memory import telemetry
from backend.services.memory.agent import vault_tools
from backend.services.memory.agent.memory_agent import MemoryAgentResult
from backend.services.memory.base import DayWriteOutcome
from backend.services.memory.providers import chronicle


class _Span:
    def __init__(self):
        self.attributes = {}
        self.status = None
        self.ended = False
        self.end_time = None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def end(self, end_time=None):
        self.ended = True
        self.end_time = end_time


class _Tracer:
    def __init__(self):
        self.calls = []

    def start_span(self, name, **kwargs):
        span = _Span()
        self.calls.append((name, kwargs, span))
        return span


def test_text_payload_is_metadata_only_by_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_MEMORY_CAPTURE_CONTENT", raising=False)

    payload = telemetry.text_payload("private transcript")

    assert payload["chars"] == 18
    assert len(payload["sha256"]) == 64
    assert "content" not in payload
    assert "private transcript" not in json.dumps(payload)


def test_text_payload_content_capture_is_explicit_and_bounded(monkeypatch):
    monkeypatch.setenv("LANGFUSE_MEMORY_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("LANGFUSE_MEMORY_CONTENT_MAX_CHARS", "7")

    payload = telemetry.text_payload("private transcript")

    assert payload["content"] == "private"
    assert payload["content_truncated"] is True
    assert payload["chars"] == 18


def test_memory_attempt_is_scoped_and_resets():
    assert telemetry.current_memory_attempt() == "primary"
    with telemetry.memory_attempt("recovery"):
        assert telemetry.current_memory_attempt() == "recovery"
    assert telemetry.current_memory_attempt() == "primary"


def test_memory_span_records_only_exception_type_and_ends(monkeypatch):
    tracer = _Tracer()
    monkeypatch.setattr(
        "backend.observability.otel_setup.get_tracer",
        lambda _name: tracer,
    )

    with pytest.raises(RuntimeError, match="credential-bearing provider detail"):
        with telemetry.memory_span(
            "memory_write", attributes={"chronicle.memory.executor": "pi"}
        ):
            raise RuntimeError("credential-bearing provider detail")

    _name, _kwargs, span = tracer.calls[0]
    assert span.attributes["error.type"] == "RuntimeError"
    assert span.attributes["chronicle.memory.success"] is False
    assert "credential-bearing provider detail" not in json.dumps(span.attributes)
    assert span.ended is True


def test_memory_span_start_failure_never_breaks_caller(monkeypatch):
    class BrokenTracer:
        def start_span(self, *_args, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        "backend.observability.otel_setup.get_tracer",
        lambda _name: BrokenTracer(),
    )

    with telemetry.memory_span("memory_search") as span:
        assert span is None


def test_observation_io_serializes_sanitized_payload(monkeypatch):
    monkeypatch.delenv("LANGFUSE_MEMORY_CAPTURE_CONTENT", raising=False)
    span = _Span()

    telemetry.set_observation_io(
        span,
        input={"query": telemetry.text_payload("private query")},
        output={"answer": telemetry.text_payload("private answer")},
    )

    serialized = json.dumps(span.attributes)
    assert "private query" not in serialized
    assert "private answer" not in serialized
    assert "langfuse.observation.input" in span.attributes
    assert "langfuse.observation.output" in span.attributes


def test_dense_opt_in_observation_retains_exact_sanitized_content(monkeypatch):
    monkeypatch.setenv("LANGFUSE_MEMORY_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("LANGFUSE_MEMORY_CONTENT_MAX_CHARS", "200000")
    span = _Span()

    telemetry.set_observation_io(
        span,
        input={
            "first": telemetry.text_payload("a" * 65536),
            "second": telemetry.text_payload("b" * 65536),
        },
    )

    payload = json.loads(span.attributes["langfuse.observation.input"])
    assert payload["first"]["content"] == "a" * 65536
    assert payload["second"]["content"] == "b" * 65536
    assert payload["first"]["content_truncated"] is False
    assert payload["second"]["content_truncated"] is False


def test_record_llm_usage_span_uses_child_generation_conventions(monkeypatch):
    monkeypatch.delenv("LANGFUSE_MEMORY_CAPTURE_CONTENT", raising=False)
    tracer = _Tracer()
    monkeypatch.setattr(
        "backend.observability.otel_setup.get_tracer",
        lambda _name: tracer,
    )

    telemetry.record_llm_usage_span(
        "pi_model_run",
        provider="chronicle-llamacpp",
        model="qwen-local",
        usage={"input_tokens": 12, "output_tokens": 3, "ignored": "not numeric"},
        start_time_ns=100,
        end_time_ns=200,
        input={"prompt": telemetry.text_payload("private prompt")},
        output={
            "completion": telemetry.text_payload("private completion"),
            "stop_reason": "stop",
        },
        attributes={"chronicle.memory.attempt": "primary"},
    )

    name, kwargs, span = tracer.calls[0]
    assert name == "pi_model_run"
    assert kwargs["start_time"] == 100
    assert kwargs["attributes"]["gen_ai.operation.name"] == "chat"
    assert kwargs["attributes"]["gen_ai.usage.input_tokens"] == 12
    assert "gen_ai.usage.ignored" not in kwargs["attributes"]
    assert (
        json.loads(span.attributes["langfuse.observation.input"])["prompt"]["chars"]
        == 14
    )
    assert (
        json.loads(span.attributes["langfuse.observation.output"])["stop_reason"]
        == "stop"
    )
    assert "private prompt" not in json.dumps(span.attributes)
    assert "private completion" not in json.dumps(span.attributes)
    assert span.end_time == 200


def test_no_usage_emits_no_llm_span(monkeypatch):
    monkeypatch.setattr(
        "backend.observability.otel_setup.get_tracer",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not resolve tracer")),
    )

    telemetry.record_llm_usage_span(
        "pi_model_run",
        provider="chronicle-llamacpp",
        model="qwen-local",
        usage={},
        start_time_ns=1,
        end_time_ns=2,
    )


@pytest.mark.asyncio
async def test_day_trace_uses_namespaced_session_and_typed_source_ids(monkeypatch):
    captured = {}

    @contextmanager
    def capture_span(name, *, attributes=None, parent_context=None):
        captured["name"] = name
        captured["attributes"] = dict(attributes or {})
        yield _Span()

    service = chronicle.MemoryService(
        SimpleNamespace(write_agent_backend="direct", write_recovery_backend=None)
    )
    monkeypatch.setattr(chronicle, "memory_span", capture_span)
    monkeypatch.setattr(service, "_ensure_initialized", AsyncMock())
    monkeypatch.setattr(
        service,
        "_add_day_memory_agent",
        AsyncMock(return_value=(DayWriteOutcome.COMPLETE, [])),
    )

    await service.add_day_memory(
        "A sufficiently long day digest.",
        "2026-08-06",
        "user-one",
        day_index_digest="A sufficiently long index digest.",
        source_run_id="run-one",
        source_episode_ids=["episode-one"],
        source_conversation_ids=["conversation-one"],
    )

    attributes = captured["attributes"]
    assert captured["name"] == "memory_write_day"
    assert "gen_ai.conversation.id" not in attributes
    assert attributes["session.id"] == ("timeline-day:user-one:2026-08-06:run-one")
    assert attributes["langfuse.session.id"] == attributes["session.id"]
    assert attributes["chronicle.memory.source_run_id"] == "run-one"
    assert attributes["chronicle.memory.source_episode_ids"] == ["episode-one"]
    assert attributes["chronicle.memory.source_conversation_ids"] == [
        "conversation-one"
    ]


@pytest.mark.asyncio
async def test_fallback_trace_exposes_reason_and_agent_path(tmp_path, monkeypatch):
    captured = []

    @contextmanager
    def capture_span(name, *, attributes=None, parent_context=None):
        span = _Span()
        span.attributes.update(attributes or {})
        captured.append((name, span))
        yield span

    class IncompleteAgent:
        def __init__(self, _root):
            pass

        async def run(self, _transcript, conversation_id, **_kwargs):
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=1,
                touched=[],
                summary="stopped",
                truncated=True,
            )

    service = chronicle.MemoryService(
        SimpleNamespace(
            write_agent_backend="pi",
            write_recovery_backend=None,
            review_writes=False,
        )
    )
    monkeypatch.setattr(chronicle, "memory_span", capture_span)
    monkeypatch.setattr(chronicle, "record_event_sync", lambda **_event: None)

    await service._run_agent_with_note_guarantee(
        IncompleteAgent,
        tmp_path,
        "Speaker: preserve this source.",
        "conversation-fallback-trace",
        source_date="2026-08-18T10:00:00+00:00",
    )

    fallback_span = next(
        span for name, span in captured if name == "memory_write.source_fallback"
    )
    assert fallback_span.attributes["chronicle.memory.primary_backend"] == "pi"
    assert fallback_span.attributes["chronicle.memory.recovery_backend"] == "none"
    assert fallback_span.attributes["chronicle.memory.fallback_reasons"] == [
        "invalid_note",
        "incomplete_agent",
    ]


def test_vault_tools_retain_attempt_across_dispatch_thread_boundary(
    tmp_path, monkeypatch
):
    recorded = []

    @contextmanager
    def capture_span(name, *, attributes=None, parent_context=None):
        recorded.append((name, attributes, parent_context))
        yield _Span()

    monkeypatch.setattr(vault_tools, "memory_span", capture_span)

    with telemetry.memory_attempt("recovery"):
        tools = vault_tools.VaultTools(tmp_path, trace_context="captured-parent")
    tools.dispatch("glob", {"pattern": "*.md"})

    assert recorded[0][0] == "memory_tool.glob"
    assert recorded[0][1]["chronicle.memory.attempt"] == "recovery"
    assert recorded[0][2] == "captured-parent"


def test_repeated_edit_of_same_note_is_traced_as_mutating(tmp_path, monkeypatch):
    note = tmp_path / "same-note.md"
    note.write_text("synthetic", encoding="utf-8")
    spans = []

    @contextmanager
    def capture_span(*_args, **_kwargs):
        span = _Span()
        spans.append(span)
        yield span

    tools = vault_tools.VaultTools(tmp_path)

    def repeat_mutation(_name, _args):
        tools._mark_touched("same-note.md")
        return "updated"

    monkeypatch.setattr(vault_tools, "memory_span", capture_span)
    monkeypatch.setattr(tools, "_dispatch", repeat_mutation)

    tools.dispatch("synthetic_edit", {})
    tools.dispatch("synthetic_edit", {})

    assert len(tools.touched) == 1
    assert [span.attributes["chronicle.memory.tool.mutated"] for span in spans] == [
        True,
        True,
    ]
    assert [
        span.attributes["chronicle.memory.tool.mutation_count"] for span in spans
    ] == [1, 1]


@pytest.mark.asyncio
async def test_traced_job_flushes_after_root_span_ends(monkeypatch):
    state = {"ended": False, "flushes": 0}

    class Provider:
        def force_flush(self, *, timeout_millis):
            assert timeout_millis == 5000
            assert state["ended"] is True
            state["flushes"] += 1
            return True

    class Tracer:
        @contextmanager
        def start_as_current_span(self, _name, *, attributes):
            assert attributes["chronicle.pipeline.stage"] == "memory"
            try:
                yield _Span()
            finally:
                state["ended"] = True

    monkeypatch.setattr(otel_setup, "_otel_initialised", True)
    monkeypatch.setattr(otel_setup, "_tracer_provider", Provider())
    monkeypatch.setattr(otel_setup, "get_tracer", lambda: Tracer())

    @otel_setup.traced_job("memory_extraction", pipeline_stage="memory")
    async def job(conversation_id):
        assert conversation_id == "conversation-1"
        return "complete"

    assert await job("conversation-1") == "complete"
    assert state == {"ended": True, "flushes": 1}
