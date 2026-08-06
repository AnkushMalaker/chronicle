"""Privacy and failure-isolation contracts for memory observability."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from advanced_omi_backend.observability import otel_setup
from advanced_omi_backend.services.memory import telemetry
from advanced_omi_backend.services.memory.agent import vault_tools


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
        "advanced_omi_backend.observability.otel_setup.get_tracer",
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
        "advanced_omi_backend.observability.otel_setup.get_tracer",
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


def test_oversized_opt_in_observation_remains_valid_json(monkeypatch):
    monkeypatch.setenv("LANGFUSE_MEMORY_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("LANGFUSE_MEMORY_CONTENT_MAX_CHARS", "65536")
    span = _Span()

    telemetry.set_observation_io(
        span,
        input={
            "first": telemetry.text_payload("a" * 65536),
            "second": telemetry.text_payload("b" * 65536),
        },
    )

    payload = json.loads(span.attributes["langfuse.observation.input"])
    assert payload["truncated"] is True
    assert payload["chars"] > 70000
    assert len(payload["sha256"]) == 64


def test_record_llm_usage_span_uses_child_generation_conventions(monkeypatch):
    tracer = _Tracer()
    monkeypatch.setattr(
        "advanced_omi_backend.observability.otel_setup.get_tracer",
        lambda _name: tracer,
    )

    telemetry.record_llm_usage_span(
        "pi_model_run",
        provider="chronicle-llamacpp",
        model="qwen-local",
        usage={"input_tokens": 12, "output_tokens": 3, "ignored": "not numeric"},
        start_time_ns=100,
        end_time_ns=200,
        attributes={"chronicle.memory.attempt": "primary"},
    )

    name, kwargs, span = tracer.calls[0]
    assert name == "pi_model_run"
    assert kwargs["start_time"] == 100
    assert kwargs["attributes"]["gen_ai.operation.name"] == "chat"
    assert kwargs["attributes"]["gen_ai.usage.input_tokens"] == 12
    assert "gen_ai.usage.ignored" not in kwargs["attributes"]
    assert span.end_time == 200


def test_no_usage_emits_no_llm_span(monkeypatch):
    monkeypatch.setattr(
        "advanced_omi_backend.observability.otel_setup.get_tracer",
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
