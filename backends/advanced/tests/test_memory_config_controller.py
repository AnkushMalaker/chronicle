"""Semantic validation and process reload behavior for memory configuration."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from advanced_omi_backend import chat_service
from advanced_omi_backend.controllers import system_controller


def _registry(*, api_family="openai", model_type="llm", url="http://llm.test/v1"):
    model = SimpleNamespace(
        model_type=model_type,
        api_family=api_family,
        model_params={},
        context_window=None,
        resolved_url=lambda: url,
    )
    return SimpleNamespace(
        get_by_name=lambda name: model if name == "local-qwen" else None
    )


def _pi_memory(
    *, context_window=8192, max_tokens=2048, model="local-qwen", compat=None
):
    return {
        "provider": "chronicle",
        "agents": {
            "write": {"backend": "pi", "recovery_backend": "direct"},
            "search": {"backend": "pi"},
        },
        "backends": {
            "pi": {
                "model": model,
                "context_window": context_window,
                "max_tokens": max_tokens,
                "thinking": "off",
                **({"compat": compat} if compat is not None else {}),
            }
        },
    }


def _codex_memory(*, codex_config=None, as_recovery=False):
    write = (
        {"backend": "direct", "recovery_backend": "codex"}
        if as_recovery
        else {"backend": "codex", "recovery_backend": "direct"}
    )
    return {
        "provider": "chronicle",
        "agents": {
            "write": write,
            "search": {"backend": "direct"},
        },
        "backends": {
            "codex": {} if codex_config is None else codex_config,
        },
    }


def test_memory_config_semantics_accept_valid_pi(monkeypatch):
    monkeypatch.setattr(system_controller, "get_models_registry", _registry)

    system_controller._validate_memory_mapping(_pi_memory())


def test_memory_config_semantics_validates_consecutive_pi_call_guard(monkeypatch):
    monkeypatch.setattr(system_controller, "get_models_registry", _registry)
    memory = _pi_memory()
    memory["agents"]["write"]["max_consecutive_identical_tool_calls"] = 2

    system_controller._validate_memory_mapping(memory)

    memory["agents"]["write"]["max_consecutive_identical_tool_calls"] = 0
    with pytest.raises(ValueError, match="max_consecutive_identical_tool_calls"):
        system_controller._validate_memory_mapping(memory)


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        (_pi_memory(model="missing"), "unknown registry model"),
        (
            _pi_memory(context_window=8192, max_tokens=7500),
            "leave at least 1024 tokens",
        ),
        (
            _pi_memory(compat={"supportsDeveloperRole": "yes"}),
            "supportsDeveloperRole must be a boolean",
        ),
    ],
)
def test_memory_config_semantics_reject_invalid_pi(monkeypatch, memory, message):
    monkeypatch.setattr(system_controller, "get_models_registry", _registry)

    with pytest.raises(ValueError, match=message):
        system_controller._validate_memory_mapping(memory)


@pytest.mark.parametrize("as_recovery", [False, True])
def test_memory_config_semantics_accept_valid_selected_codex(as_recovery):
    memory = _codex_memory(
        as_recovery=as_recovery,
        codex_config={
            "timeout_seconds": "900",
            "sandbox_mode": "workspace-write",
            "model": "gpt-5.6-terra",
            "reasoning_effort": " XHIGH ",
            "max_used_percent": "80",
            "limit_id": "codex",
        },
    )

    system_controller._validate_memory_mapping(memory)
    memory["backends"]["codex"]["reasoning_effort"] = "   "
    system_controller._validate_memory_mapping(memory)


@pytest.mark.parametrize(
    ("codex_config", "message"),
    [
        ([], "memory.backends.codex must be a mapping"),
        ({"timeout_seconds": 0}, "timeout_seconds must be a positive integer"),
        ({"timeout_seconds": 1.5}, "timeout_seconds must be a positive integer"),
        ({"timeout_seconds": False}, "timeout_seconds must be a positive integer"),
        ({"sandbox_mode": "outside-vault"}, "sandbox_mode must be one of"),
        ({"sandbox_mode": False}, "sandbox_mode must be one of"),
        ({"model": 123}, "model must be a string"),
        ({"reasoning_effort": False}, "reasoning_effort must be a string"),
        ({"reasoning_effort": "extreme"}, "reasoning_effort must be one of"),
        ({"max_used_percent": -1}, "max_used_percent must be an integer"),
        ({"max_used_percent": 101}, "max_used_percent must be an integer"),
        ({"max_used_percent": 1.5}, "max_used_percent must be an integer"),
        ({"max_used_percent": False}, "max_used_percent must be an integer"),
        ({"max_used_percent": "abc"}, "max_used_percent must be an integer"),
        ({"limit_id": 123}, "limit_id must be a string"),
    ],
)
def test_memory_config_semantics_reject_invalid_selected_codex(codex_config, message):
    with pytest.raises(ValueError, match=message):
        system_controller._validate_memory_mapping(
            _codex_memory(codex_config=codex_config)
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("agents", [], "memory.agents must be a mapping"),
        ("backends", [], "memory.backends must be a mapping"),
    ],
)
def test_memory_config_semantics_reject_falsy_non_mapping_sections(
    field, value, message
):
    memory = _codex_memory()
    memory[field] = value

    with pytest.raises(ValueError, match=message):
        system_controller._validate_memory_mapping(memory)


@pytest.mark.parametrize("obsolete_key", ["agent_executor", "codex", "pi"])
def test_memory_config_semantics_reject_obsolete_root_keys(obsolete_key):
    memory = {"provider": "chronicle", obsolete_key: {}}

    with pytest.raises(ValueError) as exc_info:
        system_controller._validate_memory_mapping(memory)

    message = str(exc_info.value)
    assert obsolete_key in message
    assert "setup wizard" in message
    assert "memory.agents" in message
    assert "memory.backends" in message


def test_reset_chat_service_discards_cached_memory_dependency(monkeypatch):
    cached = chat_service.ChatService()
    cached._initialized = True
    cached.memory_service = object()
    monkeypatch.setattr(chat_service, "_chat_service", cached)

    chat_service.reset_chat_service()

    assert cached._initialized is False
    assert chat_service._chat_service is None


@pytest.mark.asyncio
async def test_memory_config_save_resets_api_and_requests_worker_restart(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yml"
    config_path.write_text("memory:\n  provider: chronicle\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(system_controller, "_find_config_path", lambda: config_path)
    monkeypatch.setattr(
        system_controller,
        "load_models_config",
        lambda force_reload=False: events.append(("registry", force_reload)),
    )
    monkeypatch.setattr(
        system_controller,
        "reset_memory_service",
        lambda: events.append(("memory", True)),
    )
    monkeypatch.setattr(
        system_controller,
        "reset_chat_service",
        lambda: events.append(("chat", True)),
    )
    monkeypatch.setattr(
        system_controller,
        "signal_worker_restart",
        lambda: events.append(("workers", True)),
    )

    result = await system_controller.update_memory_config_raw("provider: chronicle\n")

    assert result["requires_worker_restart"] is True
    assert events == [
        ("registry", True),
        ("memory", True),
        ("chat", True),
        ("workers", True),
    ]


@pytest.mark.asyncio
async def test_memory_config_reload_resets_api_dependencies_and_workers(monkeypatch):
    events = []
    monkeypatch.setattr(system_controller, "_find_config_path", lambda: "/config.yml")
    monkeypatch.setattr(
        system_controller,
        "load_models_config",
        lambda force_reload=False: events.append(("registry", force_reload)),
    )
    monkeypatch.setattr(
        system_controller,
        "reset_memory_service",
        lambda: events.append(("memory", True)),
    )
    monkeypatch.setattr(
        system_controller,
        "reset_chat_service",
        lambda: events.append(("chat", True)),
    )
    monkeypatch.setattr(
        system_controller,
        "signal_worker_restart",
        lambda: events.append(("workers", True)),
    )

    result = await system_controller.reload_memory_config()

    assert result["requires_worker_restart"] is True
    assert events == [
        ("registry", True),
        ("memory", True),
        ("chat", True),
        ("workers", True),
    ]


@pytest.mark.asyncio
async def test_memory_config_save_rejects_invalid_backend_before_write(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yml"
    original = "memory:\n  provider: chronicle\n"
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(system_controller, "_find_config_path", lambda: config_path)

    with pytest.raises(HTTPException, match="Unsupported memory search backend"):
        await system_controller.update_memory_config_raw(
            "agents:\n  search:\n    backend: codex\n"
        )

    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_llm_operations_save_rejects_obsolete_memory_agent(monkeypatch):
    def _unexpected_registry_load():
        raise AssertionError("obsolete operation must fail before loading the registry")

    monkeypatch.setattr(
        system_controller, "get_models_registry", _unexpected_registry_load
    )

    with pytest.raises(HTTPException) as exc_info:
        await system_controller.save_llm_operations(
            {"memory_agent": {"model": "local-qwen"}}
        )

    assert exc_info.value.status_code == 400
    assert "llm_operations.memory_agent is obsolete" in exc_info.value.detail
    assert "memory_write" in exc_info.value.detail
    assert "memory_search" in exc_info.value.detail


@pytest.mark.asyncio
async def test_llm_operations_preserve_reasoning_effort_on_get_and_save(monkeypatch):
    operation = SimpleNamespace(
        model="local-qwen",
        temperature=0.1,
        max_tokens=4096,
        response_format=None,
        reasoning_effort="off",
    )
    registry = SimpleNamespace(
        llm_operations={"memory_write": operation},
        defaults={"llm": "local-qwen"},
        get_all_by_type=lambda _model_type: [],
        get_by_name=lambda name: object() if name == "local-qwen" else None,
    )
    saved = {}
    events = []
    monkeypatch.setattr(system_controller, "get_models_registry", lambda: registry)
    monkeypatch.setattr(
        system_controller,
        "save_config_section",
        lambda section, value: saved.update(section=section, value=value) or True,
    )
    monkeypatch.setattr(system_controller, "load_models_config", lambda **_kwargs: None)
    monkeypatch.setattr(
        system_controller,
        "reset_memory_service",
        lambda: events.append("memory"),
    )
    monkeypatch.setattr(
        system_controller,
        "reset_chat_service",
        lambda: events.append("chat"),
    )
    monkeypatch.setattr(
        system_controller,
        "signal_worker_restart",
        lambda: events.append("workers"),
    )

    current = await system_controller.get_llm_operations()
    result = await system_controller.save_llm_operations(current["operations"])

    assert current["operations"]["memory_write"]["reasoning_effort"] == "off"
    assert result["status"] == "success"
    assert result["requires_worker_restart"] is True
    assert events == ["memory", "chat", "workers"]
    assert saved == {
        "section": "llm_operations",
        "value": current["operations"],
    }


@pytest.mark.asyncio
async def test_llm_operations_reject_non_string_reasoning_effort(monkeypatch):
    registry = SimpleNamespace(get_by_name=lambda _name: object())
    monkeypatch.setattr(system_controller, "get_models_registry", lambda: registry)

    with pytest.raises(HTTPException, match="reasoning_effort"):
        await system_controller.save_llm_operations(
            {"memory_write": {"reasoning_effort": False}}
        )
