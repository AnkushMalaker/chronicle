"""Configuration contract for independent Chronicle memory-agent backends."""

from types import SimpleNamespace

import pytest

from backend.services.memory import config as memory_config
from backend.services.memory.agent import codex_agent, pi_agent
from backend.services.memory.agent.memory_agent import (
    DEFAULT_AGENT_SYSTEM_PROMPT,
    build_write_task,
)
from backend.services.memory.config import MemoryConfig
from backend.services.memory.providers.chronicle import MemoryService


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


def test_memory_config_loads_consecutive_pi_call_guard(monkeypatch):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {
                "write": {
                    "backend": "pi",
                    "recovery_backend": None,
                    "max_consecutive_identical_tool_calls": "2",
                },
                "search": {"backend": "direct"},
            },
        }
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    config = memory_config.build_memory_config_from_env()

    assert config.write_max_consecutive_identical_tool_calls == 2


@pytest.mark.parametrize("value", [0, -1, False, 1.5, "not-an-int"])
def test_memory_config_rejects_invalid_consecutive_pi_call_guard(monkeypatch, value):
    registry = _registry(
        {
            "provider": "chronicle",
            "agents": {
                "write": {
                    "backend": "pi",
                    "max_consecutive_identical_tool_calls": value,
                }
            },
        }
    )
    monkeypatch.setattr(memory_config, "get_models_registry", lambda: registry)

    with pytest.raises(ValueError, match="max_consecutive_identical_tool_calls"):
        memory_config.build_memory_config_from_env()


def test_memory_service_passes_consecutive_guard_only_to_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pi_agent, "pi_executor_available", lambda: (True, "/usr/local/bin/pi")
    )
    service = MemoryService(
        MemoryConfig(
            write_agent_backend="pi",
            write_max_consecutive_identical_tool_calls=2,
        )
    )

    agent = service._write_agent_instance(pi_agent.PiMemoryAgent, tmp_path / "vault")

    assert agent.max_identical_tool_calls == 2
    assert agent.terminate_on_verified is True


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


def test_all_write_prompts_forbid_about_mentions_duplication():
    for prompt in (
        DEFAULT_AGENT_SYSTEM_PROMPT,
        codex_agent.DEFAULT_CODEX_AGENT_SYSTEM_PROMPT,
    ):
        normalized = " ".join(prompt.split())
        assert "About` is for stable/current facts" in prompt
        assert "same proposition in both" in prompt
        assert "routine/background appearances" in prompt
        assert "vault owner's own Person note is not a diary" in normalized
        assert "one canonical Topic" in prompt


def test_memory_prompt_preserves_property_time_and_dates_contingent_claims():
    normalized = " ".join(DEFAULT_AGENT_SYSTEM_PROMPT.split())

    assert "`created` is immutable" in normalized
    assert (
        "`updated` must be the later of its existing value and the source date"
        in normalized
    )
    assert "Date and attribute claims whose truth may change over time" in normalized


def test_memory_prompt_forbids_repeating_empty_or_unchanged_searches():
    normalized = " ".join(DEFAULT_AGENT_SYSTEM_PROMPT.split())

    assert "`No matches found` or `Search result unchanged`" in normalized
    assert "Do not repeat or slightly vary that search" in normalized


def test_day_task_says_chronicle_owns_the_concise_episode_index():
    task = build_write_task(
        "Local day with one episode.",
        "2026-08-10",
        date="2026-08-10T00:00:00+00:00",
        record="day",
    )

    assert "Chronicle has already installed" in task
    assert "Do NOT create, rewrite, expand, or read" in task
    assert "record only what was genuinely new, decided, or" in task
    assert "owner's own Person note is not a second Daily note" in task
    assert "high bar for creating a new semantic note" in task
    assert "Never create an empty or" in task
    assert "may not invent a new organic category" in task
    assert "Do not search or read `Conversations/` or other `Daily/` notes" in task
    assert "twelve search/read calls" in task
    assert "Pi still chooses which semantic notes" in task


def test_conversation_task_scopes_existing_note_repair_to_its_source_id():
    task = build_write_task(
        "Speaker: Grounded transcript.",
        "conv-123",
        date="2026-08-18T00:00:00+00:00",
        record="conversation",
    )

    assert "Required source note: Conversations/conv-123.md" in task
    assert "never glob, audit, or read other Conversations/*.md" in task
    assert "You still choose which relevant People/Topic/category notes" in task


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
