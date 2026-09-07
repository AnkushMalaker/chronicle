import json
from types import SimpleNamespace

from backend.services import codex_langfuse


def test_official_plugin_receives_saved_rollout(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    rollout = codex_home / "sessions/2026/08/10/rollout-probe-thread-123.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("saved paid session", encoding="utf-8")
    plugin = (
        codex_home
        / "plugins/cache/codex-observability-plugin/tracing/0.1.0/dist/index.mjs"
    )
    plugin.parent.mkdir(parents=True)
    plugin.write_text("official plugin", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["payload"] = json.loads(kwargs["input"])
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_langfuse.subprocess, "run", fake_run)
    stdout = '{"type":"thread.started","thread_id":"thread-123"}\n'

    assert codex_langfuse.upload_codex_trace(stdout, operation="timeline")
    assert captured["command"] == ["node", str(plugin)]
    assert captured["payload"]["transcript_path"] == str(rollout)
    assert captured["env"]["TRACE_TO_LANGFUSE"] == "true"
    assert set(json.loads(captured["env"]["LANGFUSE_CODEX_TAGS"])) == {
        "chronicle",
        "timeline",
    }


def test_trace_upload_fails_open_without_credentials(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    assert not codex_langfuse.upload_codex_trace("", operation="memory")
