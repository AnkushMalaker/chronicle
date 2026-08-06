"""Configuration contract for independent Chronicle memory-agent backends."""

from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory import config as memory_config
from advanced_omi_backend.services.memory.agent import pi_agent
from advanced_omi_backend.services.memory.config import MemoryConfig
from advanced_omi_backend.services.memory.providers.chronicle import MemoryService


def _registry(memory, *, llm_operations=None):
    llm = SimpleNamespace(
        model_name="local-model",
        model_provider="llamacpp",
        api_key="no-key",
        model_params={"temperature": 0.2, "max_tokens": 4096},
        resolved_url=lambda: "http://llm.test/v1",
    )
    embedding = SimpleNamespace(model_name="local-embedding")
    return SimpleNamespace(
        memory=memory,
        llm_operations=llm_operations or {},
        get_default=lambda model_type: llm if model_type == "llm" else embedding,
    )


def test_memory_config_selects_pi_independently_for_write_and_search(monkeypatch):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {
                "write": {"backend": "pi", "recovery_backend": "direct"},
                "search": {"backend": "pi"},
            },
        }
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    config = memory_config.build_memory_config_from_env()

    assert config.write_agent_backend == "pi"
    assert config.write_recovery_backend == "direct"
    assert config.search_agent_backend == "pi"


def test_memory_config_rejects_codex_for_search(monkeypatch):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {
                "write": {"backend": "direct"},
                "search": {"backend": "codex"},
            },
        }
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    with pytest.raises(ValueError, match="Unsupported memory search backend: codex"):
        memory_config.build_memory_config_from_env()


def test_memory_config_allows_disabling_write_recovery(monkeypatch):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {
                "write": {"backend": "pi", "recovery_backend": None},
                "search": {"backend": "direct"},
            },
        }
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    config = memory_config.build_memory_config_from_env()

    assert config.write_recovery_backend is None


@pytest.mark.parametrize("legacy_key", ["agent_executor", "codex", "pi"])
def test_memory_config_rejects_obsolete_flat_executor_keys(monkeypatch, legacy_key):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {"write": {"backend": "direct"}},
            legacy_key: "codex" if legacy_key == "agent_executor" else {},
        }
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    with pytest.raises(ValueError, match=r"Obsolete flat memory configuration.*wizard"):
        memory_config.build_memory_config_from_env()


def test_memory_config_rejects_obsolete_memory_agent_operation(monkeypatch):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {"write": {"backend": "direct"}},
        },
        llm_operations={"memory_agent": SimpleNamespace(model="openai")},
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    with pytest.raises(ValueError, match=r"llm_operations\.memory_agent.*wizard"):
        memory_config.build_memory_config_from_env()


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        ({"agents": []}, "memory.agents must be a mapping"),
        ({"agents": {"write": []}}, "memory.agents.write/search must be mappings"),
        ({"agents": {"search": []}}, "memory.agents.write/search must be mappings"),
        ({"extraction": []}, "memory.extraction must be a mapping"),
    ],
)
def test_memory_config_rejects_falsy_non_mapping_sections(monkeypatch, memory, message):
    monkeypatch.setattr(
        memory_config,
        "get_models_registry",
        lambda: _registry({"provider": "chronicle", **memory}),
    )

    with pytest.raises(ValueError, match=message):
        memory_config.build_memory_config_from_env()


@pytest.mark.asyncio
async def test_memory_service_readiness_resolves_full_pi_configuration(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pi_agent, "pi_executor_available", lambda: (True, "/usr/local/bin/pi")
    )

    def invalid(operation, *, force_fallback=False):
        calls.append((operation, force_fallback))
        raise pi_agent.PiExecutorError("unknown registry model")

    monkeypatch.setattr(pi_agent, "validate_pi_executor_config", invalid)
    service = MemoryService(
        MemoryConfig(
            write_agent_backend="pi",
            write_recovery_backend=None,
            search_agent_backend="direct",
        )
    )

    with pytest.raises(pi_agent.PiExecutorError, match="unknown registry model"):
        await service.initialize()
    assert await service.test_connection() is False
    assert calls == [("memory_write", False), ("memory_write", False)]
