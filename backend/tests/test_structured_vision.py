import base64
from types import SimpleNamespace

import pytest

from backend.model_registry import AppModels, LLMOperationConfig, ModelDef
from backend.services.vision.structured_vision import (
    VisionUnavailable,
    run_structured_vision,
    vision_route_identity,
)


def _registry(*, capabilities=("vision",)) -> AppModels:
    model = ModelDef(
        name="qwen",
        model_type="llm",
        model_provider="llamacpp",
        model_name="qwen.gguf",
        model_url="http://llama-cpp-llm:8080/v1",
        capabilities=list(capabilities),
    )
    return AppModels(
        defaults={"llm": model.name},
        models={model.name: model},
        llm_operations={
            "image_test": LLMOperationConfig(
                model=model.name,
                response_format="json",
                reasoning_effort="none",
            )
        },
    )


def test_vision_route_identity_exposes_effective_model(monkeypatch):
    monkeypatch.setattr(
        "backend.services.vision.structured_vision.get_models_registry",
        _registry,
    )

    assert (
        vision_route_identity({"backend": "model", "operation": "image_test"})
        == "model:image_test:llamacpp:qwen:qwen.gguf"
    )
    assert (
        vision_route_identity({"backend": "codex", "codex": {"model": "gpt-5.6-luna"}})
        == "codex:gpt-5.6-luna"
    )


@pytest.mark.asyncio
async def test_model_vision_uses_named_operation_and_data_url(monkeypatch):
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"description":"a dog"}')
                )
            ]
        )

    monkeypatch.setattr(
        "backend.services.vision.structured_vision.get_models_registry",
        _registry,
    )
    monkeypatch.setattr(
        "backend.services.vision.structured_vision.async_chat_with_tools",
        fake_chat,
    )

    result = await run_structured_vision(
        "Describe it",
        [("dog.png", b"png")],
        {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
            "additionalProperties": False,
        },
        {"backend": "model", "operation": "image_test", "timeout_seconds": 12},
    )

    assert result == {"description": "a dog"}
    messages, kwargs = calls[0]
    assert kwargs == {"operation": "image_test", "timeout_seconds": 12}
    content = messages[0]["content"]
    assert content[0]["type"] == "text"
    assert "required JSON schema" in content[0]["text"]
    assert content[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"png").decode("ascii")
    )


@pytest.mark.asyncio
async def test_model_vision_rejects_operation_without_vision_capability(monkeypatch):
    monkeypatch.setattr(
        "backend.services.vision.structured_vision.get_models_registry",
        lambda: _registry(capabilities=()),
    )

    with pytest.raises(VisionUnavailable, match="not configured for vision"):
        await run_structured_vision(
            "Describe it",
            [("dog.jpg", b"jpg")],
            {"type": "object"},
            {"backend": "model", "operation": "image_test"},
        )


@pytest.mark.asyncio
async def test_model_vision_retries_schema_invalid_json_once(monkeypatch):
    responses = iter(['{"wrong":true}', '{"description":"fixed"}'])
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)))]
        )

    monkeypatch.setattr(
        "backend.services.vision.structured_vision.get_models_registry",
        _registry,
    )
    monkeypatch.setattr(
        "backend.services.vision.structured_vision.async_chat_with_tools",
        fake_chat,
    )

    result = await run_structured_vision(
        "Describe it",
        [("dog.jpg", b"jpg")],
        {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
        {"backend": "model", "operation": "image_test"},
    )

    assert result == {"description": "fixed"}
    assert len(calls) == 2
    assert "did not match the schema" in calls[1][-1]["content"]
