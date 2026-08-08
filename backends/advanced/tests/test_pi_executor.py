"""Pi memory executor: config resolution, isolation, JSONL, and vault-tool bridge."""

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.agent import pi_agent, vault_tools
from advanced_omi_backend.services.memory.agent.pi_agent import (
    PiExecutorError,
    PiMemoryAgent,
    _parse_events,
    _PiRuntimeConfig,
    search_vault_with_pi,
)


@contextlib.contextmanager
def _no_lock(_user_id):
    yield


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(vault_tools, "vault_note_lock", _no_lock)


def _runtime_config(
    *,
    api_key="super-secret-key",
    base_url="http://kraken:8083/v1",
    temperature=0.2,
    timeout_seconds=30,
):
    return _PiRuntimeConfig(
        binary="/opt/pi/bin/pi",
        provider="chronicle-llamacpp",
        model="qwen3.6-27b",
        base_url=base_url,
        api_key=api_key,
        thinking="low",
        max_tokens=4096,
        context_window=8192,
        timeout_seconds=timeout_seconds,
        reasoning=True,
        temperature=temperature,
    )


def _jsonl(*events):
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


def _gateway_credentials(extension_source):
    url_match = re.search(r"^const gatewayUrl = (.+);$", extension_source, re.MULTILINE)
    token_match = re.search(
        r"^const bearerToken = (.+);$", extension_source, re.MULTILINE
    )
    assert url_match and token_match
    return json.loads(url_match.group(1)), json.loads(token_match.group(1))


def _call_gateway(extension_source, name, arguments):
    url, token = _gateway_credentials(extension_source)
    request = urllib.request.Request(
        url,
        data=json.dumps({"name": name, "arguments": arguments}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _report_gateway_limit(extension_source, reason):
    url, token = _gateway_credentials(extension_source)
    request = urllib.request.Request(
        url.removesuffix("/tool") + "/limit",
        data=json.dumps({"reason": reason}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


def test_pi_subprocess_env_forces_loopback_proxy_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "models.internal,.tailnet")
    monkeypatch.setenv("no_proxy", "existing.internal")

    env = pi_agent._pi_subprocess_env(tmp_path)

    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert env["NO_PROXY"] == env["no_proxy"]
    assert env["NO_PROXY"].split(",") == [
        "models.internal",
        ".tailnet",
        "existing.internal",
        "127.0.0.1",
        "localhost",
        "::1",
    ]


def _fake_spawn(captured, *, events, tool_call=None, returncode=0, stderr=b""):
    async def spawn(*command, **kwargs):
        command = list(command)
        extension_path = Path(command[command.index("-e") + 1])
        agent_dir = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
        captured.update(
            command=command,
            kwargs=kwargs,
            extension=extension_path.read_text(encoding="utf-8"),
            extension_mode=extension_path.stat().st_mode & 0o777,
            models=json.loads((agent_dir / "models.json").read_text(encoding="utf-8")),
            models_mode=(agent_dir / "models.json").stat().st_mode & 0o777,
            system_prompt=(agent_dir / "system-prompt.md").read_text(encoding="utf-8"),
            system_prompt_mode=(agent_dir / "system-prompt.md").stat().st_mode & 0o777,
        )

        class Process:
            def __init__(self):
                self.returncode = returncode
                self.killed = False
                self.waited = False

            async def communicate(self, input_bytes=None):
                captured["stdin"] = input_bytes.decode() if input_bytes else ""
                if tool_call is not None:
                    name, arguments = tool_call
                    captured["gateway_result"] = _call_gateway(
                        captured["extension"], name, arguments
                    )
                return _jsonl(*events), stderr

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True
                return self.returncode

        return Process()

    return spawn


def _successful_events(*, summary, tool_name, usage):
    return [
        {"type": "session", "version": 3, "id": "test"},
        {"type": "agent_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": tool_name,
                        "arguments": {},
                    }
                ],
                "usage": usage,
                "stopReason": "toolUse",
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": tool_name,
            "args": {},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": tool_name,
            "result": {"content": [{"type": "text", "text": "ok"}]},
            "isError": False,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": summary}],
                "usage": {"input": 5, "output": 2, "totalTokens": 7},
                "stopReason": "stop",
            },
        },
        {"type": "agent_end", "messages": []},
    ]


def test_pi_executor_availability_only_checks_binary(monkeypatch):
    monkeypatch.setenv("PI_BINARY", "custom-pi")
    monkeypatch.setattr(
        pi_agent.shutil,
        "which",
        lambda value: "/bin/pi" if value == "custom-pi" else None,
    )

    assert pi_agent.pi_executor_available() == (True, "/bin/pi")

    monkeypatch.setattr(pi_agent.shutil, "which", lambda _value: None)
    assert pi_agent.pi_executor_available() == (
        False,
        "Pi binary 'custom-pi' not found on PATH",
    )


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        ({"backends": []}, "memory.backends must be a mapping"),
        ({"backends": {"pi": []}}, "memory.backends.pi must be a mapping"),
    ],
)
def test_pi_settings_reject_falsy_non_mapping_sections(memory, message):
    registry = SimpleNamespace(memory=memory)

    with pytest.raises(PiExecutorError, match=message):
        pi_agent._pi_settings(registry)


def test_config_uses_exact_pi_backend_and_registry_operation(monkeypatch):
    model_def = SimpleNamespace(
        name="qwen-registry-entry",
        model_provider="Llama.cpp Local",
        api_family="openai",
        thinking=True,
        model_params={"context_window": 16384},
    )
    resolved = SimpleNamespace(
        model_def=model_def,
        model_name="upstream/qwen:Q4_K_M",
        base_url="http://kraken:8083/v1",
        api_key=None,
        max_tokens=9000,
        reasoning_effort="medium",
        temperature=0.37,
    )
    calls = []
    registry = SimpleNamespace(
        memory={
            "backends": {
                "pi": {
                    "model": "qwen-registry-entry",
                    "thinking": "low",
                    "timeout_seconds": 77,
                    "context_window": 8192,
                    "max_tokens": 4096,
                }
            },
            # A similarly named legacy path must not be read.
            "pi": {"model": "wrong-model"},
        },
        get_llm_operation=lambda operation, model_override=None: (
            calls.append((operation, model_override)) or resolved
        ),
    )
    monkeypatch.setattr(pi_agent, "get_models_registry", lambda: registry)
    monkeypatch.setattr(
        pi_agent, "pi_executor_available", lambda: (True, "/usr/local/bin/pi")
    )

    config = pi_agent._resolve_pi_config("memory_write")

    assert calls == [("memory_write", "qwen-registry-entry")]
    assert config.provider == "chronicle-llama-cpp-local"
    assert config.model == "upstream/qwen:Q4_K_M"
    assert config.base_url == "http://kraken:8083/v1"
    assert config.api_key == "no-key"
    assert config.thinking == "low"
    assert config.max_tokens == 4096
    assert config.context_window == 8192
    assert config.timeout_seconds == 77
    assert config.temperature == 0.37
    assert config.compat == {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "qwen-chat-template",
    }


def test_config_accepts_max_thinking_and_explicit_compat_overrides(monkeypatch):
    model_def = SimpleNamespace(
        name="remote-model",
        model_provider="custom-openai",
        api_family="openai",
        thinking=True,
        model_params={
            "pi_compat": {
                "thinkingFormat": "deepseek",
                "supportsDeveloperRole": True,
            }
        },
    )
    resolved = SimpleNamespace(
        model_def=model_def,
        model_name="deepseek-r1",
        base_url="https://models.example/v1",
        api_key="secret",
        max_tokens=2048,
        reasoning_effort="high",
        temperature=0.41,
    )
    registry = SimpleNamespace(
        memory={
            "backends": {
                "pi": {
                    "thinking": "max",
                    "compat": {"maxTokensField": "max_completion_tokens"},
                }
            }
        },
        get_llm_operation=lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(pi_agent, "get_models_registry", lambda: registry)
    monkeypatch.setattr(pi_agent, "pi_executor_available", lambda: (True, "/bin/pi"))

    config = pi_agent._resolve_pi_config("memory_write")

    assert config.thinking == "max"
    assert config.compat == {
        "supportsDeveloperRole": True,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_completion_tokens",
        "thinkingFormat": "deepseek",
    }


def test_public_config_validator_resolves_requested_operation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: calls.append(
            (operation, force_fallback)
        ),
    )

    assert (
        pi_agent.validate_pi_executor_config("memory_search", force_fallback=True)
        is None
    )
    assert calls == [("memory_search", True)]


def test_models_payload_escapes_pi_config_value_syntax():
    config = _runtime_config(api_key="!printf compromised-$HOME-$$")

    payload = pi_agent._models_payload(config)

    assert payload["providers"]["chronicle-llamacpp"]["apiKey"] == (
        "$!printf compromised-$$HOME-$$$$"
    )


def test_config_rejects_output_budget_without_prompt_headroom(monkeypatch):
    model_def = SimpleNamespace(
        name="qwen",
        model_provider="llamacpp",
        api_family="openai",
        thinking=True,
        model_params={},
    )
    resolved = SimpleNamespace(
        model_def=model_def,
        model_name="qwen",
        base_url="http://localhost/v1",
        api_key="none-needed",
        max_tokens=7500,
        reasoning_effort="low",
        temperature=0.2,
    )
    registry = SimpleNamespace(
        memory={"backends": {"pi": {"context_window": 8192, "max_tokens": 7500}}},
        get_llm_operation=lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(pi_agent, "get_models_registry", lambda: registry)
    monkeypatch.setattr(pi_agent, "pi_executor_available", lambda: (True, "/bin/pi"))

    with pytest.raises(PiExecutorError, match="leave at least 1024 tokens"):
        pi_agent._resolve_pi_config("memory_write")


@pytest.mark.parametrize("value", [True, 1.5, "1.5", 0, -1])
def test_config_rejects_non_positive_or_non_integral_limits(value):
    with pytest.raises(PiExecutorError, match="positive integer"):
        pi_agent._positive_int(value, name="timeout_seconds", default=900)


@pytest.mark.parametrize("value", [True, 1.5, "1.5", 0, -1])
def test_run_rejects_non_positive_or_non_integral_limits(value):
    with pytest.raises(PiExecutorError, match="positive integer"):
        pi_agent._positive_run_limit(value, name="Pi max_tool_rounds")


def test_gateway_derives_mutating_tools_from_canonical_schema_difference():
    write_tools = {
        schema["function"]["name"] for schema in vault_tools.VAULT_TOOL_SCHEMAS
    }
    read_only_tools = {
        schema["function"]["name"] for schema in vault_tools.VAULT_SEARCH_TOOL_SCHEMAS
    }

    assert write_tools - read_only_tools
    # verify_vault is write-set-only because a retrieval run has nothing to verify, not
    # because it mutates — the gateway must not serialise or audit it as a write.
    assert pi_agent._MUTATING_VAULT_TOOLS == (
        write_tools - read_only_tools - {"verify_vault"}
    )
    assert "verify_vault" not in pi_agent._MUTATING_VAULT_TOOLS


@pytest.mark.asyncio
async def test_gateway_shutdown_does_not_block_event_loop(tmp_path, monkeypatch):
    gateway = pi_agent._VaultToolGateway(
        tmp_path / "user",
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    order = []
    original_close = gateway._close

    def slow_close():
        time.sleep(0.08)
        order.append("close")
        original_close()

    monkeypatch.setattr(gateway, "_close", slow_close)

    async def heartbeat():
        await asyncio.sleep(0.01)
        order.append("heartbeat")

    await asyncio.gather(gateway.aclose(), heartbeat())

    assert order == ["heartbeat", "close"]


@pytest.mark.asyncio
async def test_gateway_shutdown_waits_off_loop_for_active_mutation(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    original_dispatch = gateway.tools.dispatch

    def delayed_dispatch(name, arguments):
        started.set()
        release.wait(timeout=2)
        return original_dispatch(name, arguments)

    monkeypatch.setattr(gateway.tools, "dispatch", delayed_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "write_note",
            {
                "path": "Conversations/late.md",
                "content": "must never land after shutdown",
            },
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)

    # Shutdown waits for the mutation gate in a worker thread, not on the event loop.
    assert not close_task.done()
    assert not (root / "Conversations" / "late.md").exists()
    release.set()
    await close_task

    response = await request_task
    assert response["result"].startswith("Wrote Conversations/late.md")
    assert (root / "Conversations" / "late.md").read_text() == (
        "must never land after shutdown"
    )
    assert gateway.tools.touched == {"Conversations/late.md"}

    rejected = gateway.dispatch(
        "write_note",
        {"path": "Conversations/too-late.md", "content": "must not land"},
    )
    assert rejected == "Error: Pi vault gateway is closing"
    assert not (root / "Conversations" / "too-late.md").exists()


@pytest.mark.asyncio
async def test_gateway_shutdown_captures_active_mutation_error_before_returning(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()

    def failing_dispatch(_name, _arguments):
        started.set()
        release.wait(timeout=2)
        raise vault_tools.VaultToolError("active mutation failed")

    monkeypatch.setattr(gateway.tools, "dispatch", failing_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "write_note",
            {"path": "Conversations/failed.md", "content": "must not land"},
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)
    assert not close_task.done()

    release.set()
    await close_task

    # The error is captured while the mutation gate is still held, so callers may
    # safely assemble the result as soon as close returns.
    assert gateway.errors == ["write_note: active mutation failed"]
    response = await request_task
    assert response == {"result": "Error: active mutation failed"}
    assert not (root / "Conversations" / "failed.md").exists()


@pytest.mark.asyncio
async def test_gateway_shutdown_records_admitted_read_before_audit_freeze(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    original_dispatch = gateway.tools.dispatch

    def delayed_dispatch(name, arguments):
        started.set()
        release.wait(timeout=2)
        return original_dispatch(name, arguments)

    monkeypatch.setattr(gateway.tools, "dispatch", delayed_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "read_note",
            {"path": "People/Alice.md"},
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)
    assert not close_task.done()
    release.set()
    await close_task

    assert gateway.read_notes == {"People/Alice.md": "Alice prefers tea."}
    assert await request_task == {"result": "Alice prefers tea."}


@pytest.mark.asyncio
async def test_gateway_drain_timeout_freezes_late_read_audit(tmp_path, monkeypatch):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    monkeypatch.setattr(pi_agent, "_GATEWAY_DRAIN_TIMEOUT_SECONDS", 0.01)
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    original_dispatch = gateway.tools.dispatch

    def delayed_dispatch(name, arguments):
        started.set()
        release.wait(timeout=2)
        return original_dispatch(name, arguments)

    monkeypatch.setattr(gateway.tools, "dispatch", delayed_dispatch)
    request_task = asyncio.create_task(
        asyncio.to_thread(
            _call_gateway,
            extension,
            "read_note",
            {"path": "People/Alice.md"},
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    await gateway.aclose()

    assert gateway.read_notes == {}
    assert gateway.close_error == (
        "Pi vault gateway requests did not drain within 0.01 seconds; "
        "vault mutations are complete, but read/error details may be omitted"
    )
    errors_at_return = list(gateway.errors)

    release.set()
    assert await request_task == {"result": "Alice prefers tea."}
    assert gateway.read_notes == {}
    assert gateway.errors == errors_at_return


@pytest.mark.asyncio
async def test_gateway_shutdown_preserves_admitted_limit_signal(tmp_path, monkeypatch):
    gateway = pi_agent._VaultToolGateway(
        tmp_path / "user",
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=1,
    )
    gateway.__enter__()
    extension = pi_agent._extension_source(
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        gateway_url=gateway.url,
        token=gateway.token,
    )
    started = threading.Event()
    release = threading.Event()
    admitted_values = []
    original_set_limit = gateway.set_limit

    def delayed_set_limit(reason, *, admitted=False):
        admitted_values.append(admitted)
        started.set()
        release.wait(timeout=2)
        original_set_limit(reason, admitted=admitted)

    monkeypatch.setattr(gateway, "set_limit", delayed_set_limit)
    request_task = asyncio.create_task(
        asyncio.to_thread(_report_gateway_limit, extension, "hard limit reached")
    )
    assert await asyncio.to_thread(started.wait, 1)

    close_task = asyncio.create_task(gateway.aclose())
    await asyncio.sleep(0.02)
    assert not close_task.done()
    release.set()
    await close_task

    assert await request_task == 204
    assert admitted_values == [True]
    assert gateway.limit_error == "hard limit reached"


def test_gateway_tool_call_cap_is_atomic_under_concurrency(tmp_path):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    gateway = pi_agent._VaultToolGateway(
        root,
        vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
        max_tool_calls=2,
    )

    def read_note():
        try:
            return gateway.dispatch("read_note", {"path": "People/Alice.md"})
        except pi_agent._PiLimitExceeded:
            return "limited"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: read_note(), range(8)))

    assert results.count("Alice prefers tea.") == 2
    assert results.count("limited") == 6
    assert gateway.call_count == 2
    assert gateway.limit_error and "tool-call limit exceeded (2)" in gateway.limit_error


@pytest.mark.asyncio
async def test_search_rejects_nonpositive_hard_round_limit_before_spawn(tmp_path):
    with pytest.raises(PiExecutorError, match="search max_rounds"):
        await search_vault_with_pi("question", tmp_path / "user", max_rounds=0)


@pytest.mark.asyncio
async def test_write_uses_isolated_canonical_gateway_and_reports_audit_state(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    content = "A canonical conversation note."
    events = _successful_events(
        summary="Recorded the conversation.",
        tool_name="write_note",
        usage={
            "input": 100,
            "output": 20,
            "cacheRead": 10,
            "cacheWrite": 3,
            "reasoning": 4,
            "totalTokens": 120,
        },
    )
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle write system prompt"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setenv("CHRONICLE_SECRET_SHOULD_NOT_LEAK", "backend-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=events,
            tool_call=(
                "write_note",
                {"path": "Conversations/conv-1.md", "content": content},
            ),
        ),
    )

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-1")

    assert (root / "Conversations" / "conv-1.md").read_text() == content
    assert result.touched == ["Conversations/conv-1.md"]
    assert result.removed == []
    assert result.summary == "Recorded the conversation."
    assert result.rounds == 2
    assert result.tool_calls == 1
    assert result.usage == {
        "input_tokens": 105,
        "output_tokens": 22,
        "input_cached_tokens": 10,
        "input_cache_write_tokens": 3,
        "output_reasoning_tokens": 4,
        "total_tokens": 127,
    }
    assert result.errors == []
    assert not result.truncated

    command = captured["command"]
    for flag in (
        "--mode",
        "--no-session",
        "--offline",
        "--no-context-files",
        "--no-extensions",
        "--no-prompt-templates",
        "--no-themes",
        "--no-builtin-tools",
        "--tools",
    ):
        assert flag in command
    # A tool-using run is taught the vault's shape by a generated skill rather than by
    # more system prompt, so skills are loaded, not disabled.
    assert "--no-skills" not in command
    assert "--skill" in command
    assert "--api-key" not in command
    assert "super-secret-key" not in command
    assert "Chronicle write system prompt" not in command
    assert command[command.index("--tools") + 1] == ",".join(
        schema["function"]["name"] for schema in vault_tools.VAULT_TOOL_SCHEMAS
    )
    assert "import " not in captured["extension"]
    assert "pi.registerTool" in captured["extension"]
    assert captured["extension_mode"] == 0o600
    assert captured["models_mode"] == 0o600
    assert captured["system_prompt_mode"] == 0o600
    assert captured["system_prompt"] == "Chronicle write system prompt"
    assert captured["models"]["providers"]["chronicle-llamacpp"]["apiKey"] == (
        "super-secret-key"
    )
    assert captured["kwargs"]["env"]["PI_OFFLINE"] == "1"
    assert captured["kwargs"]["env"]["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert "CHRONICLE_SECRET_SHOULD_NOT_LEAK" not in captured["kwargs"]["env"]
    assert "Speaker: hello" in captured["stdin"]


@pytest.mark.asyncio
async def test_search_returns_only_notes_read_through_canonical_tools(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    captured = {}
    events = _successful_events(
        summary="Alice prefers tea (People/Alice.md).",
        tool_name="read_note",
        usage={"input": 30, "output": 1, "totalTokens": 31},
    )
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle search system prompt"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=events,
            tool_call=("read_note", {"path": "People/Alice.md"}),
        ),
    )

    result = await search_vault_with_pi("What does Alice prefer?", root)

    assert result.answer == "Alice prefers tea (People/Alice.md)."
    assert result.notes == [
        {"path": "People/Alice.md", "content": "Alice prefers tea."}
    ]
    assert result.rounds == 2
    assert result.usage["input_tokens"] == 35
    assert result.errors == []
    assert captured["command"][captured["command"].index("--tools") + 1] == (
        "grep,glob,read_note,search_images"
    )


@pytest.mark.asyncio
async def test_search_limit_uses_pi_again_without_tools_for_final_synthesis(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    config = _runtime_config()
    calls = []

    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: config,
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle search system prompt"

    async def invoke(_root, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                pi_agent._PiEventResult(
                    rounds=7,
                    usage={"input_tokens": 20},
                    errors=[
                        "Pi tool-round limit exceeded (6)",
                        "Pi JSONL stream ended without agent_end",
                        "Pi completed without a final assistant message",
                    ],
                    fatal_errors=[
                        "Pi tool-round limit exceeded (6)",
                        "Pi JSONL stream ended without agent_end",
                        "Pi completed without a final assistant message",
                    ],
                    truncated=True,
                ),
                SimpleNamespace(
                    limit_error="Pi tool-round limit exceeded (6)",
                    call_count=0,
                    read_notes={
                        "Topics/Synthetic.md": "A synthetic fact for retrieval."
                    },
                ),
            )
        return (
            pi_agent._PiEventResult(
                summary="The synthetic fact is recorded.",
                rounds=1,
                usage={"input_tokens": 10, "output_tokens": 4},
                agent_ended=True,
            ),
            SimpleNamespace(limit_error=None, call_count=0, read_notes={}),
        )

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(pi_agent, "_invoke_pi", invoke)

    result = await search_vault_with_pi("What was recorded?", root)

    assert result.answer == "The synthetic fact is recorded."
    assert result.notes == [
        {
            "path": "Topics/Synthetic.md",
            "content": "A synthetic fact for retrieval.",
        }
    ]
    assert result.rounds == 8
    assert result.usage == {"input_tokens": 30, "output_tokens": 4}
    assert result.errors == []
    assert result.warnings == [
        "Pi tool-round limit exceeded (6)",
        "Pi JSONL stream ended without agent_end",
        "Pi completed without a final assistant message",
    ]
    assert calls[0]["schemas"] is pi_agent.VAULT_SEARCH_TOOL_SCHEMAS
    assert calls[1]["schemas"] == ()
    assert "Note evidence JSON" in calls[1]["prompt"]
    assert "no tools are available" in calls[1]["system_prompt"].lower()


@pytest.mark.asyncio
async def test_search_preserves_audited_failure_when_final_synthesis_raises(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    config = _runtime_config(api_key="private-test-key")
    calls = 0

    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: config,
    )

    async def prompt(*_args, **_kwargs):
        return "Chronicle search system prompt"

    async def invoke(_root, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                pi_agent._PiEventResult(
                    rounds=7,
                    errors=["Pi tool-round limit exceeded (6)"],
                    fatal_errors=["Pi tool-round limit exceeded (6)"],
                    truncated=True,
                ),
                SimpleNamespace(
                    limit_error="Pi tool-round limit exceeded (6)",
                    call_count=0,
                    read_notes={"Topics/Synthetic.md": "Audited evidence."},
                ),
            )
        raise pi_agent.PiExecutorError("failed with private-test-key")

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(pi_agent, "_invoke_pi", invoke)

    result = await search_vault_with_pi("What was recorded?", root)

    assert calls == 2
    assert result.answer == pi_agent.PI_SEARCH_FAILURE_ANSWER
    assert result.notes == [
        {"path": "Topics/Synthetic.md", "content": "Audited evidence."}
    ]
    assert result.rounds == 7
    assert result.errors == [
        "Pi tool-round limit exceeded (6)",
        "Pi final search synthesis failed: PiExecutorError: failed with [REDACTED]",
    ]


@pytest.mark.asyncio
async def test_no_tool_pi_invocation_loads_temperature_runtime_extension(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}

    async def spawn(*command, **kwargs):
        captured["command"] = list(command)
        captured["agent_dir"] = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
        extension_path = Path(command[command.index("-e") + 1])
        captured["extension"] = extension_path.read_text(encoding="utf-8")
        captured["extension_mode"] = extension_path.stat().st_mode & 0o777

        class Process:
            returncode = 0

            async def communicate(self, input_bytes=None):
                captured["stdin"] = input_bytes.decode()
                return (
                    _jsonl(
                        {"type": "agent_start"},
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "final answer"}],
                                "stopReason": "stop",
                            },
                        },
                        {"type": "agent_end", "messages": []},
                    ),
                    b"",
                )

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        return Process()

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    events, gateway = await pi_agent._invoke_pi(
        root,
        prompt="final prompt",
        system_prompt="final system",
        schemas=(),
        config=_runtime_config(temperature=0.37),
        max_tool_rounds=1,
        max_tool_calls=1,
    )

    assert events.summary == "final answer"
    assert events.truncated is False
    assert gateway.call_count == 0
    assert "-e" in captured["command"]
    assert "--tools" not in captured["command"]
    assert "before_provider_request" in captured["extension"]
    assert "const temperature = 0.37;" in captured["extension"]
    assert "pi.registerTool" in captured["extension"]
    assert captured["extension_mode"] == 0o600
    assert captured["stdin"] == "final prompt"


def test_parse_events_preserves_tool_and_protocol_errors():
    parsed = _parse_events(
        "\n".join(
            [
                json.dumps({"type": "agent_start"}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "read_note",
                        "result": {
                            "content": [{"type": "text", "text": "network failed"}]
                        },
                        "isError": True,
                    }
                ),
                json.dumps(
                    {
                        "type": "extension_error",
                        "extensionPath": "/tmp/chronicle-vault-tools.mjs",
                        "event": "tool_call",
                        "error": "hook crashed",
                    }
                ),
                "not-json",
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
    )

    assert parsed.errors == ["read_note: network failed"]
    assert any("invalid JSONL" in error for error in parsed.fatal_errors)
    assert any(
        "Pi extension error during tool_call: hook crashed" in error
        for error in parsed.fatal_errors
    )
    assert any("without a final assistant" in error for error in parsed.fatal_errors)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required by Pi")
def test_generated_extension_aborts_on_first_tool_beyond_round_limit(tmp_path):
    extension_path = tmp_path / "chronicle-vault-tools.mjs"
    extension_path.write_text(
        pi_agent._extension_source(
            vault_tools.VAULT_SEARCH_TOOL_SCHEMAS,
            gateway_url="http://127.0.0.1:1/tool",
            token="test-token",
            temperature=0.37,
            max_tool_rounds=2,
            max_tool_calls=8,
        ),
        encoding="utf-8",
    )
    harness = f"""
import extension from {json.dumps(extension_path.as_uri())};
const handlers = {{}};
const tools = [];
const reports = [];
globalThis.fetch = async (url, options) => {{
  reports.push({{ url, body: JSON.parse(options.body) }});
  return {{ ok: true, status: 204, text: async () => "" }};
}};
extension({{
  on(name, handler) {{ handlers[name] = handler; }},
  registerTool(tool) {{ tools.push(tool); }},
}});
const originalPayload = {{ model: "qwen", messages: [] }};
const providerPayload = await handlers.before_provider_request(
  {{ payload: originalPayload }},
  {{}},
);
if (providerPayload.temperature !== 0.37) {{
  throw new Error(`expected temperature 0.37, got ${{providerPayload.temperature}}`);
}}
if (providerPayload.model !== "qwen" || providerPayload.messages !== originalPayload.messages) {{
  throw new Error("provider hook did not preserve the original payload");
}}
if (Object.hasOwn(originalPayload, "temperature")) {{
  throw new Error("provider hook mutated the original payload");
}}
let aborted = 0;
const context = {{ abort() {{ aborted += 1; }} }};
handlers.turn_start();
await handlers.tool_call({{}}, context);
await handlers.tool_call({{}}, context);
handlers.turn_start();
await handlers.tool_call({{}}, context);
handlers.turn_start();
const blocked = await handlers.tool_call({{}}, context);
// Derived, not hardcoded: the contract is that the extension registers exactly the
// schemas it was handed, which is what stops a write tool leaking into search.
if (tools.length !== {len(vault_tools.VAULT_SEARCH_TOOL_SCHEMAS)}) {{
  throw new Error(`expected {len(vault_tools.VAULT_SEARCH_TOOL_SCHEMAS)} tools, got ${{tools.length}}`);
}}
if (tools.some((tool) => tool.executionMode !== "sequential")) {{
  throw new Error("all Chronicle tools must execute sequentially");
}}
if (aborted !== 1) throw new Error(`expected one abort, got ${{aborted}}`);
if (!blocked?.block) throw new Error("limit call was not blocked");
if (blocked.reason !== "Pi tool-round limit exceeded (2)") throw new Error(blocked.reason);
if (reports.length !== 1 || reports[0].body.reason !== blocked.reason) {{
  throw new Error("limit was not reported to Chronicle");
}}
"""

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_parse_events_accepts_a_provider_error_that_pi_retried_successfully():
    parsed = _parse_events(
        "\n".join(
            json.dumps(event)
            for event in [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "temporarily unavailable",
                    },
                },
                {"type": "agent_end", "messages": []},
                {"type": "auto_retry_start", "attempt": 1},
                {"type": "auto_retry_end", "success": True, "attempt": 1},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "recovered"}],
                        "stopReason": "stop",
                    },
                },
                {"type": "agent_end", "messages": []},
            ]
        )
    )

    assert parsed.summary == "recovered"
    assert parsed.fatal_errors == []
    assert parsed.errors == [
        "recovered after Pi assistant error: temporarily unavailable"
    ]


@pytest.mark.asyncio
async def test_fatal_pi_event_returns_search_error_and_preserves_read_notes(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    note = root / "People" / "Alice.md"
    note.parent.mkdir(parents=True)
    note.write_text("Alice prefers tea.", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "search"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=[
                {"type": "agent_start"},
                {"type": "error", "message": "provider disconnected: super-secret-key"},
                {"type": "agent_end", "messages": []},
            ],
            tool_call=("read_note", {"path": "People/Alice.md"}),
        ),
    )

    result = await search_vault_with_pi("question", root)

    assert result.answer == "(Pi search failed before completing)"
    assert result.notes == [
        {"path": "People/Alice.md", "content": "Alice prefers tea."}
    ]
    assert any("provider disconnected: [REDACTED]" in error for error in result.errors)
    assert all("super-secret-key" not in error for error in result.errors)


@pytest.mark.asyncio
async def test_write_returns_auditable_truncated_result_after_nonzero_exit(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    content = "Written before Pi failed."
    proxy_url = "http://proxy-user:proxy-pass@proxy.internal:3128"
    model_url = "https://model-user:model-pass@models.internal/v1"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(base_url=model_url),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="The write landed before shutdown.",
                tool_name="write_note",
                usage={"input": 1, "output": 1, "totalTokens": 2},
            ),
            tool_call=(
                "write_note",
                {"path": "Conversations/conv-failed.md", "content": content},
            ),
            returncode=17,
            stderr=(
                "upstream rejected super-secret-key via "
                f"{proxy_url} proxy-pass and model-pass"
            ).encode(),
        ),
    )

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-failed")

    assert (root / "Conversations" / "conv-failed.md").read_text() == content
    assert result.touched == ["Conversations/conv-failed.md"]
    assert result.summary == "The write landed before shutdown."
    assert result.truncated
    assert any("status 17" in error for error in result.errors)
    assert any("[REDACTED]" in error for error in result.errors)
    assert all("super-secret-key" not in error for error in result.errors)
    assert all("proxy-pass" not in error for error in result.errors)
    assert all("model-pass" not in error for error in result.errors)


@pytest.mark.asyncio
async def test_nonzero_exit_after_rename_preserves_removed_audit_state(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    old_note = root / "People" / "Alice.md"
    old_note.parent.mkdir(parents=True)
    old_content = "# Alice\n\n## About\n- Prefers tea.\n"
    old_note.write_text(old_content, encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="Renamed before failure.",
                tool_name="rename_person",
                usage={},
            ),
            tool_call=("rename_person", {"old_name": "Alice", "new_name": "Alicia"}),
            returncode=23,
        ),
    )

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-rename")

    assert not old_note.exists()
    assert (root / "People" / "Alicia.md").read_text(encoding="utf-8") == old_content
    assert result.touched == ["People/Alicia.md"]
    assert result.removed == [
        {
            "old_path": "People/Alice.md",
            "new_path": "People/Alicia.md",
            "before": old_content,
        }
    ]
    assert result.truncated
    assert any("status 23" in error for error in result.errors)


@pytest.mark.asyncio
async def test_timeout_preserves_prior_write_and_waits_for_killed_process(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.01),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*command, **kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        extension = extension_path.read_text(encoding="utf-8")

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()
                self.called = False

            async def communicate(self, input_bytes=None):
                if not self.called:
                    self.called = True
                    _call_gateway(
                        extension,
                        "write_note",
                        {
                            "path": "Conversations/conv-timeout.md",
                            "content": "Durable before timeout.",
                        },
                    )
                await self.released.wait()
                return _jsonl({"type": "agent_start"}), b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-timeout")

    assert (root / "Conversations" / "conv-timeout.md").read_text() == (
        "Durable before timeout."
    )
    assert result.touched == ["Conversations/conv-timeout.md"]
    assert result.truncated
    assert any("timed out" in error for error in result.errors)
    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_cancellation_kills_and_waits_for_pi_subprocess(tmp_path, monkeypatch):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    started = asyncio.Event()
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*_command, **_kwargs):
        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()

            async def communicate(self, input_bytes=None):
                started.set()
                await self.released.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    task = asyncio.create_task(PiMemoryAgent(root).run("Speaker: hello", "conv-cancel"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_cancellation_during_timeout_cleanup_is_repropagated_after_reap(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    cleanup_started = asyncio.Event()
    allow_drain = asyncio.Event()
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.01),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*_command, **_kwargs):
        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False

            async def communicate(self, input_bytes=None):
                await allow_drain.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                cleanup_started.set()

            async def wait(self):
                self.waited = True
                await allow_drain.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    task = asyncio.create_task(
        PiMemoryAgent(root).run("Speaker: hello", "conv-cleanup")
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_drain.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_cancellation_during_gateway_close_waits_for_close_then_propagates(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(summary="done", tool_name="write_note", usage={}),
        ),
    )
    original_aclose = pi_agent._VaultToolGateway.aclose

    async def delayed_aclose(self):
        close_started.set()
        await allow_close.wait()
        await original_aclose(self)

    monkeypatch.setattr(pi_agent._VaultToolGateway, "aclose", delayed_aclose)

    task = asyncio.create_task(PiMemoryAgent(root).run("Speaker: hello", "conv-close"))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_invoke_reconciles_limit_admitted_during_gateway_close(
    tmp_path, monkeypatch
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="done",
                tool_name="write_note",
                usage={},
            ),
        ),
    )
    original_aclose = pi_agent._VaultToolGateway.aclose

    async def limit_during_close(self):
        self.set_limit("hard limit admitted during close", admitted=True)
        await original_aclose(self)

    monkeypatch.setattr(pi_agent._VaultToolGateway, "aclose", limit_during_close)

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-limit-close")

    assert result.truncated
    assert result.errors.count("hard limit admitted during close") == 1


@pytest.mark.asyncio
async def test_write_tool_call_limit_rejects_extra_mutation_and_terminates_pi(
    tmp_path, monkeypatch, unlocked
):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(pi_agent, "MAX_PI_WRITE_TOOL_CALLS", 2, raising=False)
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.2),
    )

    async def prompt(*_args, **_kwargs):
        return "write"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*command, **_kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        extension = extension_path.read_text(encoding="utf-8")

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()

            async def communicate(self, input_bytes=None):
                for index in range(3):
                    try:
                        _call_gateway(
                            extension,
                            "write_note",
                            {
                                "path": f"Conversations/{index}.md",
                                "content": f"note {index}",
                            },
                        )
                    except urllib.error.HTTPError as exc:
                        captured["limit_status"] = exc.code
                        captured["limit_body"] = exc.read().decode()
                        await self.released.wait()
                        return _jsonl({"type": "agent_start"}), b""
                return (
                    _jsonl(
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "unbounded"}],
                                "stopReason": "stop",
                            },
                        },
                        {"type": "agent_end", "messages": []},
                    ),
                    b"",
                )

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    result = await PiMemoryAgent(root).run("Speaker: hello", "conv-limit")

    assert sorted(path.name for path in (root / "Conversations").glob("*.md")) == [
        "0.md",
        "1.md",
    ]
    assert captured["limit_status"] == 429
    assert result.truncated
    assert any("tool-call limit exceeded (2)" in error for error in result.errors)
    assert captured["process"].killed
    assert captured["process"].waited


@pytest.mark.asyncio
async def test_search_tool_round_limit_signal_terminates_pi(tmp_path, monkeypatch):
    root = tmp_path / "user"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(
        pi_agent,
        "_resolve_pi_config",
        lambda operation, force_fallback=False: _runtime_config(timeout_seconds=0.2),
    )

    async def prompt(*_args, **_kwargs):
        return "search"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)

    async def spawn(*command, **_kwargs):
        extension_path = Path(command[command.index("-e") + 1])
        extension = extension_path.read_text(encoding="utf-8")
        captured["extension"] = extension

        class Process:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False
                self.released = asyncio.Event()

            async def communicate(self, input_bytes=None):
                try:
                    captured["limit_status"] = _report_gateway_limit(
                        extension, "Pi tool-round limit exceeded (2)"
                    )
                except urllib.error.HTTPError as exc:
                    captured["limit_status"] = exc.code
                    return (
                        _jsonl(
                            {
                                "type": "message_end",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "unbounded"}],
                                    "stopReason": "stop",
                                },
                            },
                            {"type": "agent_end", "messages": []},
                        ),
                        b"",
                    )
                await self.released.wait()
                return _jsonl({"type": "agent_start"}), b""

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

            async def wait(self):
                self.waited = True
                await self.released.wait()
                return self.returncode

        process = Process()
        captured["process"] = process
        return process

    monkeypatch.setattr(pi_agent.asyncio, "create_subprocess_exec", spawn)

    result = await search_vault_with_pi("question", root, max_rounds=2)

    assert 'pi.on("turn_start"' in captured["extension"]
    assert captured["limit_status"] == 204
    assert result.answer == "(Pi search failed before completing)"
    assert any("tool-round limit exceeded (2)" in error for error in result.errors)
    assert captured["process"].killed
    assert captured["process"].waited
