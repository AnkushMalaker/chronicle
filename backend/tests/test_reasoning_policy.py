"""Enforce local reasoning permission at real direct and Pi request boundaries."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from omegaconf import OmegaConf

from backend import llm_client
from backend.controllers import system_controller
from backend.model_registry import (
    AppModels,
    LLMOperationConfig,
    ModelDef,
    ResolvedLLMOperation,
)
from backend.model_routes import effective_operation_routes
from backend.services.memory.agent import pi_agent
from backend.services.timeline.pi_executor import _with_reasoning_strength


def registry():
    models = {
        name: ModelDef(
            name=name,
            model_name=f"Qwen-{name}",
            model_type="llm",
            model_provider="llamacpp",
            model_url="http://localhost:8080/v1",
            thinking=True,
            model_params={"reasoning_effort": "high", "max_tokens": 2000},
        )
        for name in ("primary", "fallback")
    }
    return AppModels(
        defaults={"llm": "primary", "fallback_llm": "fallback"},
        models=models,
        memory={"backends": {"pi": {"thinking": "high"}}},
        llm_operations={"memory_write": LLMOperationConfig(reasoning_effort="high")},
    )


@pytest.mark.parametrize("operation", [None, "memory_write", "a_future_operation"])
async def test_direct_generation_always_transmits_disabled_thinking(
    monkeypatch, operation
):
    r = registry()
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]
        )
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(llm_client, "get_models_registry", lambda: r)
    monkeypatch.setattr(
        ResolvedLLMOperation, "get_client", lambda *args, **kwargs: client
    )
    assert (
        await llm_client.async_generate("Return JSON", operation=operation)
        == '{"ok":true}'
    )
    assert (
        create.call_args.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"]
        is False
    )


def test_every_default_operation_and_fallback_obeys_model_policy():
    r = registry()
    defaults = OmegaConf.load(
        Path(__file__).resolve().parents[2] / "config" / "defaults.yml"
    )
    r.llm_operations = {
        name: LLMOperationConfig(**OmegaConf.to_container(value))
        for name, value in defaults.llm_operations.items()
    }
    for name in [*r.llm_operations, "not_yet_configured"]:
        primary = r.get_llm_operation(name, model_override="primary")
        fallback = r.get_fallback_llm_operation(name, primary=primary)
        for operation in (primary, fallback):
            assert (
                operation.to_api_params()["extra_body"]["chat_template_kwargs"][
                    "enable_thinking"
                ]
                is False
            ), name


def test_pi_resolution_and_stage_override_obey_model_permission(monkeypatch):
    monkeypatch.setattr(pi_agent, "get_models_registry", registry)
    monkeypatch.setattr(pi_agent, "pi_executor_available", lambda: (True, "pi"))
    for fallback in (False, True):
        config = pi_agent._resolve_pi_config("memory_write", force_fallback=fallback)
        assert config.thinking == "off"
        assert not config.reasoning_allowed
        assert replace(config, thinking="high").thinking == "off"
        assert _with_reasoning_strength(config, "high").thinking == "off"


def test_pi_outgoing_payload_cannot_reenable_disabled_thinking():
    extension = pi_agent._extension_source(
        (),
        gateway_url="http://localhost/tool",
        token="test-only",
        disable_thinking=True,
    )
    script = (
        extension.replace("export default function (pi)", "function install(pi)") + """
const handlers = {};
install({on: (name, handler) => { handlers[name] = handler; }});
console.log(JSON.stringify(handlers.before_provider_request({payload: {model: 'test', chat_template_kwargs: {enable_thinking: true, other: 1}}})));
"""
    )
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False, "other": 1}
    assert payload["model"] == "test"


def test_diagnostics_show_requested_and_effective_reasoning():
    row = effective_operation_routes(registry())[0]
    assert row["requested_reasoning_effort"] == "high"
    assert row["reasoning_effort"] == "none"
    assert row["reasoning_policy"] == "off"


async def test_admin_model_probe_uses_the_same_reasoning_policy(monkeypatch):
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hello"))]
        )
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(system_controller, "get_models_registry", registry)
    monkeypatch.setattr(
        ResolvedLLMOperation, "get_client", lambda *args, **kwargs: client
    )
    result = await system_controller.test_llm_model("primary")
    assert result["success"] is True
    assert (
        create.call_args.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"]
        is False
    )


@pytest.mark.parametrize("effort", [None, "none", "low"])
def test_pi_override_requires_operation_opt_in_even_when_model_permits(effort):
    r = registry()
    r.models["primary"].reasoning_policy = "per_operation"
    r.llm_operations["memory_write"].reasoning_effort = effort
    operation = r.get_llm_operation("memory_write")
    assert pi_agent._thinking_level("high", operation) == (
        "high" if effort == "low" else "off"
    )
