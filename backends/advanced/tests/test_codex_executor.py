"""Codex CLI memory-agent executor: selection, filesystem-diff auditing, failure paths."""

import contextlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.agent import (
    codex_agent,
    codex_quota,
    memory_agent,
)
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


def _fake_codex_run(
    vault_root, *, summary="Recorded the conversation.", returncode=0, usage=None
):
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
        turn = {"type": "turn.completed"}
        if usage is not None:
            turn["usage"] = usage
        stdout = (
            '{"type":"item.completed","item":{"item_type":"command_execution"}}\n'
            + json.dumps(turn)
            + "\n"
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


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------


def test_parse_events_sums_turn_usage():
    stdout = (
        '{"type":"turn.completed","usage":{"input_tokens":1200,'
        '"cached_input_tokens":900,"output_tokens":40}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":300,'
        '"cached_input_tokens":100,"output_tokens":10,'
        '"reasoning_output_tokens":7}}\n'
    )

    _, turns, _, usage = CodexMemoryAgent._parse_events(stdout)

    assert turns == 2
    assert usage == {
        "input_tokens": 1500,
        "input_cached_tokens": 1000,
        "output_tokens": 50,
        "output_reasoning_tokens": 7,
    }


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.completed"},  # older CLI: no usage block at all
        {"type": "turn.completed", "usage": None},
        {"type": "turn.completed", "usage": "unexpected"},
        {"type": "turn.completed", "usage": {"input_tokens": "many"}},
    ],
)
def test_turn_usage_tolerates_missing_or_odd_shapes(event):
    """The CLI's field names are not a stable contract; usage must never break a run."""
    assert CodexMemoryAgent._turn_usage(event) == {}


@pytest.mark.asyncio
async def test_run_reports_usage_from_the_json_stream(tmp_path, monkeypatch, unlocked):
    root = _seed_vault(tmp_path)
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (True, "/usr/bin/codex")
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_codex_run(root, usage={"input_tokens": 8000, "output_tokens": 120}),
    )

    result = await CodexMemoryAgent(root).run("a real transcript", "conv1")

    assert result.usage == {"input_tokens": 8000, "output_tokens": 120}


def test_usage_span_is_a_child_not_the_agent_span(monkeypatch):
    """Langfuse drops usage on ``invoke_agent`` spans, so it must ride a child LLM span.

    Pins why usage lives on ``codex_turn``: moved onto ``codex_memory_agent``
    (``gen_ai.operation.name: invoke_agent``), current Langfuse's OTEL processor
    ingests the tokens as zero without erroring anywhere. Langfuse 3.x has no such
    guard, so that regression would not show up on an older deployment.
    """
    recorded = {}

    class _Span:
        def end(self, end_time=None):
            recorded["end_time"] = end_time

    class _Tracer:
        def start_span(self, name, attributes=None, start_time=None):
            recorded.update(name=name, attributes=attributes, start_time=start_time)
            return _Span()

    monkeypatch.setattr(
        "advanced_omi_backend.observability.otel_setup.get_tracer",
        lambda _name: _Tracer(),
    )

    CodexMemoryAgent._record_usage_span(
        {"input_tokens": 10, "input_cached_tokens": 4}, "gpt-5.6-terra", 111, 222
    )

    assert recorded["name"] == "codex_turn"
    attrs = recorded["attributes"]
    assert attrs["gen_ai.operation.name"] == "chat"  # NOT invoke_agent
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.input_cached_tokens"] == 4
    # Explicit timestamps: the span is created only after the subprocess returns.
    assert (recorded["start_time"], recorded["end_time"]) == (111, 222)


def test_no_usage_emits_no_span(monkeypatch):
    def _boom(_name):
        raise AssertionError("tracer must not be built when there is no usage")

    monkeypatch.setattr(
        "advanced_omi_backend.observability.otel_setup.get_tracer", _boom
    )

    CodexMemoryAgent._record_usage_span({}, "gpt-5.6-terra", 1, 2)


# ---------------------------------------------------------------------------
# Quota guard
# ---------------------------------------------------------------------------

# Verbatim shape of a real `account/rateLimits/read` reply (codex-cli 0.144.4),
# trimmed to the fields the guard reads. Two buckets, one exhausted, one untouched.
REAL_RATE_LIMITS = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {
            "usedPercent": 100,
            "windowDurationMins": 10080,
            "resetsAt": 1785612921,
        },
        "secondary": None,
        "planType": "prolite",
        "rateLimitReachedType": "rate_limit_reached",
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 100,
                "windowDurationMins": 10080,
                "resetsAt": 1785612921,
            },
        },
        "codex_bengalfox": {
            "limitId": "codex_bengalfox",
            "limitName": "GPT-5.3-Codex-Spark",
            "primary": {
                "usedPercent": 0,
                "windowDurationMins": 10080,
                "resetsAt": 1785798988,
            },
        },
    },
}


def test_bucket_used_percent_reads_the_default_and_named_buckets():
    assert codex_quota.bucket_used_percent(REAL_RATE_LIMITS) == 100
    assert codex_quota.bucket_used_percent(REAL_RATE_LIMITS, "codex") == 100
    # A different model's bucket can be untouched while the default is exhausted.
    assert codex_quota.bucket_used_percent(REAL_RATE_LIMITS, "codex_bengalfox") == 0


def test_unknown_bucket_is_unknown_not_the_default_bucket():
    """Must not silently gate on some other budget's headroom."""
    assert codex_quota.bucket_used_percent(REAL_RATE_LIMITS, "codex_nope") is None


@pytest.mark.parametrize("payload", [None, {}, {"rateLimits": {"primary": {}}}])
def test_bucket_used_percent_unknown_shapes(payload):
    assert codex_quota.bucket_used_percent(payload) is None


def test_quota_span_attributes_carry_window_and_reset():
    attrs = codex_quota.quota_span_attributes(REAL_RATE_LIMITS)
    assert attrs["chronicle.memory.quota.used_percent"] == 100
    assert attrs["chronicle.memory.quota.window_minutes"] == 10080
    assert attrs["chronicle.memory.quota.resets_at"] == 1785612921
    assert codex_quota.quota_span_attributes(None) == {}


@pytest.mark.parametrize(
    "settings,used,expect_block",
    [
        ({"max_used_percent": 80}, 100, True),  # over budget -> yield
        ({"max_used_percent": 80}, 80, True),  # at budget -> yield
        ({"max_used_percent": 80}, 79, False),
        ({}, 100, False),  # unconfigured -> guard off
        ({"max_used_percent": None}, 100, False),
        ({"max_used_percent": "abc"}, 100, False),  # unparseable -> guard off
        ({"max_used_percent": 80}, None, False),  # unreadable -> fail OPEN
    ],
)
def test_quota_guard_decision(monkeypatch, settings, used, expect_block):
    monkeypatch.setattr(codex_agent, "_codex_settings", lambda: settings)
    monkeypatch.setattr(codex_quota, "read_rate_limits", lambda **_: {"stub": True})
    monkeypatch.setattr(codex_quota, "bucket_used_percent", lambda *_a, **_k: used)

    _, blocked = CodexMemoryAgent._check_quota("conv1")

    assert blocked is expect_block


@pytest.mark.asyncio
async def test_exhausted_quota_records_via_direct_agent_instead(
    tmp_path, monkeypatch, unlocked
):
    """Yielding must still record the conversation, not drop it."""
    root = _seed_vault(tmp_path)
    monkeypatch.setattr(
        codex_agent, "codex_executor_available", lambda: (True, "/usr/bin/codex")
    )
    monkeypatch.setattr(
        codex_agent, "_codex_settings", lambda: {"max_used_percent": 80}
    )
    monkeypatch.setattr(codex_quota, "read_rate_limits", lambda **_: REAL_RATE_LIMITS)

    def _no_subprocess(*_a, **_k):
        raise AssertionError("codex must not be spawned when over budget")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)

    delegated = {}

    class _Direct:
        def __init__(self, root, *a, **kw):
            delegated["constructed"] = True
            # The yield is a budget decision, not a failed run: it must NOT use the
            # note-guarantee recovery path's forced fallback LLM.
            delegated["force_fallback"] = kw.get("force_fallback", False)

        async def run(self, transcript, conversation_id, **kwargs):
            delegated["conversation_id"] = conversation_id
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=1,
                touched=["Conversations/conv1.md"],
                summary="recorded by the direct agent",
            )

    monkeypatch.setattr(memory_agent, "MemoryAgent", _Direct)

    result = await CodexMemoryAgent(root).run("a real transcript", "conv1")

    assert delegated["constructed"] is True
    assert delegated["force_fallback"] is False
    assert result.touched == ["Conversations/conv1.md"]
    assert not result.truncated


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
