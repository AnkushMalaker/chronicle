from types import SimpleNamespace

from backend.services.memory import audit


def test_active_trace_context_uses_langfuse_compatible_hex_ids(monkeypatch):
    context = SimpleNamespace(
        is_valid=True,
        trace_id=int("80e0efcedf25753e95a36487a0d01b28", 16),
        span_id=int("f3cc96ca98049f28", 16),
    )
    span = SimpleNamespace(get_span_context=lambda: context)
    monkeypatch.setattr(audit.trace, "get_current_span", lambda: span)

    assert audit._active_trace_context() == {
        "otel_trace_id": "80e0efcedf25753e95a36487a0d01b28",
        "otel_span_id": "f3cc96ca98049f28",
    }


def test_invalid_trace_context_is_not_recorded(monkeypatch):
    context = SimpleNamespace(is_valid=False)
    span = SimpleNamespace(get_span_context=lambda: context)
    monkeypatch.setattr(audit.trace, "get_current_span", lambda: span)

    assert audit._active_trace_context() == {}
