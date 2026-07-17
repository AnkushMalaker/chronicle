"""Codex CLI memory-agent executor: selection, filesystem-diff auditing, failure paths."""

import contextlib
import subprocess
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.agent import codex_agent, memory_agent
from advanced_omi_backend.services.memory.agent.codex_agent import CodexMemoryAgent
from advanced_omi_backend.services.memory.agent.memory_agent import (
    MemoryAgent,
    MemoryAgentResult,
)
from advanced_omi_backend.services.memory.config import MemoryConfig
from advanced_omi_backend.services.memory.providers.chronicle import MemoryService


@contextlib.contextmanager
def _no_lock(_user_id, ttl_seconds=0):
    yield


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(
        "advanced_omi_backend.services.memory.vault_lock.vault_run_lock", _no_lock
    )


# ---------------------------------------------------------------------------
# Executor selection (chronicle._agent_class)
# ---------------------------------------------------------------------------


def test_agent_class_defaults_to_direct():
    service = MemoryService(MemoryConfig())
    assert service._agent_class() is MemoryAgent


def test_agent_class_uses_codex_when_available(monkeypatch):
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (True, "/usr/bin/codex")
    )
    service = MemoryService(MemoryConfig(agent_executor="codex"))
    assert service._agent_class() is CodexMemoryAgent


def test_agent_class_falls_back_when_codex_unavailable(monkeypatch):
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (False, "no binary")
    )
    service = MemoryService(MemoryConfig(agent_executor="codex"))
    assert service._agent_class() is MemoryAgent


# ---------------------------------------------------------------------------
# CodexMemoryAgent.run
# ---------------------------------------------------------------------------


def _fake_codex_run(vault_root, *, summary="Recorded the conversation.", returncode=0):
    """A subprocess.run stand-in that mimics one codex exec editing the vault."""

    def fake_run(cmd, **kwargs):
        # Simulate the agent's edits: create the conversation note, update a
        # person note, retire a topic note.
        (vault_root / "Conversations").mkdir(exist_ok=True)
        (vault_root / "Conversations" / "conv1.md").write_text("recorded")
        (vault_root / "People" / "Old.md").write_text("updated content")
        (vault_root / "Topics" / "Gone.md").unlink()
        last_msg = cmd[cmd.index("--output-last-message") + 1]
        with open(last_msg, "w") as f:
            f.write(summary)
        stdout = (
            '{"type":"item.completed","item":{"item_type":"command_execution"}}\n'
            '{"type":"turn.completed"}\n'
        )
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return fake_run


def _seed_vault(tmp_path):
    root = tmp_path / "user1"
    (root / "People").mkdir(parents=True)
    (root / "Topics").mkdir()
    (root / "People" / "Old.md").write_text("original content")
    (root / "Topics" / "Gone.md").write_text("doomed note")
    return root


@pytest.mark.asyncio
async def test_run_derives_touched_and_removed_from_fs_diff(
    tmp_path, monkeypatch, unlocked
):
    root = _seed_vault(tmp_path)
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (True, "/usr/bin/codex")
    )
    monkeypatch.setattr(subprocess, "run", _fake_codex_run(root))

    result = await CodexMemoryAgent(root).run("a real transcript", "conv1")

    assert result.touched == ["Conversations/conv1.md", "People/Old.md"]
    assert result.removed == [
        {"old_path": "Topics/Gone.md", "new_path": "", "before": "doomed note"}
    ]
    assert result.summary == "Recorded the conversation."
    assert result.tool_calls == 1
    assert result.rounds == 1
    assert not result.truncated
    assert result.errors == []


@pytest.mark.asyncio
async def test_run_failure_is_truncated_with_errors(tmp_path, monkeypatch, unlocked):
    root = _seed_vault(tmp_path)
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (True, "/usr/bin/codex")
    )

    def failing_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", failing_run)

    result = await CodexMemoryAgent(root).run("a real transcript", "conv1")

    assert result.truncated
    assert any("timed out" in e for e in result.errors)
    assert result.touched == []  # nothing was written


@pytest.mark.asyncio
async def test_run_unavailable_executor_returns_truncated(tmp_path, monkeypatch):
    root = _seed_vault(tmp_path)
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (False, "no auth")
    )

    result = await CodexMemoryAgent(root).run("a real transcript", "conv1")

    assert result.truncated
    assert result.errors == ["codex executor unavailable: no auth"]


@pytest.mark.asyncio
async def test_force_fallback_delegates_to_direct_agent(tmp_path, monkeypatch):
    root = _seed_vault(tmp_path)
    seen = {}

    class FakeDirectAgent:
        def __init__(
            self, vault_root, operation="memory_agent", *, force_fallback=False
        ):
            seen["force_fallback"] = force_fallback

        async def run(self, transcript, conversation_id, **kwargs):
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=1,
                touched=["Conversations/conv1.md"],
                summary="fallback ran",
            )

    monkeypatch.setattr(memory_agent, "MemoryAgent", FakeDirectAgent)

    result = await CodexMemoryAgent(root, force_fallback=True).run(
        "a real transcript", "conv1"
    )

    assert seen["force_fallback"] is True
    assert result.summary == "fallback ran"
