from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import llm_client


def _registry():
    model = SimpleNamespace(
        api_key="test-key",
        model_name="test-model",
        resolved_url=lambda: "https://example.test/v1",
    )
    return SimpleNamespace(
        defaults={"llm": "primary", "fast_llm": "fast"},
        get_by_name=lambda name: model if name == "fast" else None,
    )


@pytest.mark.asyncio
async def test_named_llm_health_retries_once_after_timeout(monkeypatch):
    list_models = AsyncMock(side_effect=[TimeoutError, object()])
    client = SimpleNamespace(models=SimpleNamespace(list=list_models))
    monkeypatch.setattr(llm_client, "get_models_registry", _registry)
    monkeypatch.setattr(llm_client, "create_openai_client", lambda **_kwargs: client)

    result = await llm_client.async_health_check_fast()

    assert result["healthy"] is True
    assert list_models.await_count == 2


@pytest.mark.asyncio
async def test_named_llm_health_reports_timeout_after_retry(monkeypatch):
    list_models = AsyncMock(side_effect=TimeoutError)
    client = SimpleNamespace(models=SimpleNamespace(list=list_models))
    monkeypatch.setattr(llm_client, "get_models_registry", _registry)
    monkeypatch.setattr(llm_client, "create_openai_client", lambda **_kwargs: client)

    result = await llm_client.async_health_check_fast()

    assert result["healthy"] is False
    assert result["status"] == "❌ Connection Timeout"
    assert list_models.await_count == 2
