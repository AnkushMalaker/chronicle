"""Focused tests for the read-only retrieval benchmark harness."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from advanced_omi_backend import model_registry

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import evaluate_memory_retrieval as retrieval


def test_load_questions_supports_json_and_jsonl_with_exact_text(tmp_path):
    values = [
        {
            "id": "q1",
            "question": "  What synthetic fact was recorded?  ",
            "vault_summary": "  Exact synthetic context.  ",
        },
        {"id": "q2", "question": "Which note supports it?"},
    ]
    json_path = tmp_path / "questions.json"
    json_path.write_text(json.dumps({"questions": values}), encoding="utf-8")
    jsonl_path = tmp_path / "questions.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(value) + "\n" for value in values), encoding="utf-8"
    )

    json_questions, json_digest = retrieval.load_questions(json_path, "default context")
    jsonl_questions, _ = retrieval.load_questions(jsonl_path, "default context")

    assert json_questions == jsonl_questions
    assert json_questions[0].question == "  What synthetic fact was recorded?  "
    assert json_questions[0].vault_summary == "  Exact synthetic context.  "
    assert json_questions[1].vault_summary == "default context"
    assert json_digest == retrieval._sha256_file(json_path)


def test_load_questions_requires_unique_ids(tmp_path):
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps(
            [
                {"id": "duplicate", "question": "First?"},
                {"id": "duplicate", "question": "Second?"},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(retrieval.RetrievalInputError, match="unique"):
        retrieval.load_questions(path)


def test_direct_runtime_metadata_records_effective_operation_without_secrets(
    monkeypatch,
):
    operation = SimpleNamespace(
        model_name="qwen-upstream",
        model_def=SimpleNamespace(
            name="qwen-registry", model_provider="llamacpp", thinking=True
        ),
        api_key="secret-direct-key",
        base_url="http://private-direct-endpoint/v1",
        to_api_params=lambda: {
            "model": "qwen-upstream",
            "temperature": 0.2,
            "max_tokens": 2048,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )
    registry = SimpleNamespace(get_llm_operation=lambda name: operation)
    monkeypatch.setattr(model_registry, "get_models_registry", lambda: registry)

    metadata = retrieval._runtime_metadata("direct")

    assert metadata == {
        "operation": "memory_search",
        "model_name": "qwen-upstream",
        "model_provider": "llamacpp",
        "registry_model": "qwen-registry",
        "thinking_model": True,
        "operation_params": {
            "model": "qwen-upstream",
            "temperature": 0.2,
            "max_tokens": 2048,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    }
    serialized = json.dumps(metadata)
    assert "secret-direct-key" not in serialized
    assert "private-direct-endpoint" not in serialized


def test_pi_runtime_metadata_records_resolved_override_without_secrets(monkeypatch):
    registry = object()
    monkeypatch.setattr(model_registry, "get_models_registry", lambda: registry)
    resolved = SimpleNamespace(
        model="unsloth/Qwen3.6-27B-GGUF:Q4_K_M",
        provider="llamacpp",
        context_window=32768,
        max_tokens=4096,
        temperature=0.13,
        thinking="off",
        reasoning=True,
        timeout_seconds=900,
        compat={
            "supportsDeveloperRole": False,
            "thinkingFormat": "qwen-chat-template",
        },
        api_key="secret-pi-key",
        base_url="http://private-pi-endpoint/v1",
    )

    class FakePiModule:
        @staticmethod
        def pi_executor_available():
            return True, "/opt/pi/bin/pi"

        @staticmethod
        def _pi_settings(received_registry):
            assert received_registry is registry
            return {"model": "qwen36-llm"}

        @staticmethod
        def _resolve_pi_config(operation):
            assert operation == "memory_search"
            return resolved

    metadata = retrieval._runtime_metadata("pi", FakePiModule)

    assert metadata == {
        "operation": "memory_search",
        "executor_available": True,
        "executor_detail": "/opt/pi/bin/pi",
        "model_name": "unsloth/Qwen3.6-27B-GGUF:Q4_K_M",
        "model_provider": "llamacpp",
        "pi_model_override": "qwen36-llm",
        "pi_context_window": 32768,
        "pi_max_tokens": 4096,
        "pi_temperature": 0.13,
        "pi_thinking": "off",
        "pi_reasoning": True,
        "pi_timeout_seconds": 900,
        "pi_compat": {
            "supportsDeveloperRole": False,
            "thinkingFormat": "qwen-chat-template",
        },
    }
    serialized = json.dumps(metadata)
    assert "secret-pi-key" not in serialized
    assert "private-pi-endpoint" not in serialized


def test_manifest_output_must_be_new_and_outside_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(retrieval.RetrievalInputError, match="outside"):
        retrieval.prepare_paths(vault, vault / "manifest.json")

    output = tmp_path / "runs" / "manifest.json"
    resolved_vault, resolved_output = retrieval.prepare_paths(vault, output)
    assert resolved_vault == vault.resolve()
    assert resolved_output == output.resolve()
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700

    public_parent = tmp_path / "public-runs"
    public_parent.mkdir(mode=0o755)
    public_parent.chmod(0o755)
    with pytest.raises(retrieval.RetrievalInputError, match="must be private"):
        retrieval.prepare_paths(vault, public_parent / "manifest.json")


def test_manifest_inside_git_worktree_must_already_be_ignored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    output = repo / "private-runs" / "retrieval.json"

    with pytest.raises(retrieval.RetrievalInputError, match="not ignored by Git"):
        retrieval.prepare_paths(vault, output)

    (repo / ".gitignore").write_text("/private-runs/\n", encoding="utf-8")
    resolved_vault, resolved_output = retrieval.prepare_paths(vault, output)

    assert resolved_vault == vault.resolve()
    assert resolved_output == output.resolve()


def test_atomic_manifest_is_private_and_leaves_no_temporary_file(tmp_path):
    output_parent = tmp_path / "runs"
    output_parent.mkdir(mode=0o700)
    output = output_parent / "manifest.json"

    retrieval._atomic_write_json(output, {"private": "synthetic answer"})

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "private": "synthetic answer"
    }
    assert list(output_parent.glob(".manifest.json.*.tmp")) == []


@pytest.mark.asyncio
async def test_run_question_records_answer_references_usage_and_no_mutation(tmp_path):
    note = tmp_path / "Topics" / "Synthetic.md"
    note.parent.mkdir()
    content = "# Synthetic\n\nA fixed synthetic fact.\n"
    note.write_text(content, encoding="utf-8")
    received = {}

    async def fake_search(query, vault, **kwargs):
        received.update({"query": query, "vault": vault, **kwargs})
        return SimpleNamespace(
            answer="The fixed synthetic fact is recorded.",
            notes=[{"path": "Topics/Synthetic", "content": content}],
            rounds=2,
            errors=[],
            usage={"input_tokens": 12, "output_tokens": 4},
        )

    question = retrieval.Question("q1", "What was recorded?", "exact context", 1)
    run = await retrieval.run_question(fake_search, tmp_path, question, max_rounds=4)

    assert received == {
        "query": "What was recorded?",
        "vault": tmp_path,
        "operation": "memory_search",
        "max_rounds": 4,
        "vault_summary": "exact context",
    }
    assert run["ok"] is True
    assert run["vault_unchanged"] is True
    assert run["answer"] == "The fixed synthetic fact is recorded."
    assert run["referenced_notes"] == [
        {
            "path": "Topics/Synthetic.md",
            "valid_path": True,
            "reported_path": "Topics/Synthetic",
            "content_sha256": retrieval._sha256_text(content),
            "content_chars": len(content),
            "exists_in_vault": True,
        }
    ]
    assert run["usage"] == {"input_tokens": 12, "output_tokens": 4}


@pytest.mark.asyncio
async def test_run_question_does_not_count_pi_failure_sentinel_as_answer(tmp_path):
    async def failed_search(_query, _vault, **_kwargs):
        return SimpleNamespace(
            answer=retrieval.PI_FAILED_ANSWER,
            notes=[],
            rounds=7,
            errors=["Pi tool-round limit exceeded (6)"],
            usage={},
        )

    question = retrieval.Question("q1", "Synthetic question?", "", 1)

    run = await retrieval.run_question(
        failed_search,
        tmp_path,
        question,
        max_rounds=6,
    )

    assert run["returned"] is True
    assert run["answered"] is False
    assert run["ok"] is False


@pytest.mark.asyncio
async def test_run_question_retains_warning_without_invalidating_answer(tmp_path):
    async def recovered_search(_query, _vault, **_kwargs):
        return SimpleNamespace(
            answer="Supported synthetic answer.",
            notes=[],
            rounds=8,
            errors=[],
            warnings=["Pi tool-round limit exceeded (6)"],
            usage={},
        )

    question = retrieval.Question("q1", "Synthetic question?", "", 1)

    run = await retrieval.run_question(
        recovered_search,
        tmp_path,
        question,
        max_rounds=6,
    )

    assert run["answered"] is True
    assert run["ok"] is True
    assert run["errors"] == []
    assert run["warnings"] == ["Pi tool-round limit exceeded (6)"]


@pytest.mark.asyncio
async def test_run_question_detects_and_reports_vault_mutation(tmp_path):
    (tmp_path / "Topics").mkdir()

    async def mutating_search(_query, vault, **_kwargs):
        (vault / "Topics" / "Unexpected.md").write_text("changed", encoding="utf-8")
        return SimpleNamespace(
            answer="Changed it.", notes=[], rounds=1, errors=[], usage={}
        )

    question = retrieval.Question("q1", "Do not mutate anything.", "", 1)
    run = await retrieval.run_question(
        mutating_search, tmp_path, question, max_rounds=2
    )

    assert run["ok"] is False
    assert run["vault_unchanged"] is False
    assert run["vault_diff"] == {
        "created": ["Topics/Unexpected.md"],
        "modified": [],
        "removed": [],
    }


def test_snapshot_detects_permission_only_changes(tmp_path):
    note = tmp_path / "Topics" / "Synthetic.md"
    note.parent.mkdir()
    note.write_text("synthetic", encoding="utf-8")
    note.chmod(0o600)
    before = retrieval.snapshot_tree(tmp_path)

    note.chmod(0o644)
    after = retrieval.snapshot_tree(tmp_path)

    assert retrieval.snapshot_diff(before, after) == {
        "created": [],
        "modified": ["Topics/Synthetic.md"],
        "removed": [],
    }


def _benchmark_args(vault: Path, questions: Path, output: Path):
    return retrieval._parse_args(
        [
            "--executor",
            "direct",
            "--vault",
            str(vault),
            "--questions",
            str(questions),
            "--output",
            str(output),
        ]
    )


@pytest.mark.asyncio
async def test_benchmark_uses_private_ephemeral_copy_and_preserves_source(
    tmp_path, monkeypatch
):
    source = tmp_path / "source-vault"
    note = source / "Topics" / "Synthetic.md"
    note.parent.mkdir(parents=True)
    content = "# Synthetic\n\nA fixed synthetic fact.\n"
    note.write_text(content, encoding="utf-8")
    source_before = retrieval.snapshot_tree(source)
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps([{"id": "q1", "question": "What fact is fixed?"}]),
        encoding="utf-8",
    )
    output = tmp_path / "private-output" / "manifest.json"
    received_vaults = []

    async def fake_search(_query, vault, **_kwargs):
        received_vaults.append(vault)
        assert vault != source.resolve()
        assert stat.S_IMODE(vault.stat().st_mode) == 0o700
        copied_note = vault / "Topics" / "Synthetic.md"
        assert stat.S_IMODE(copied_note.stat().st_mode) == 0o600
        return SimpleNamespace(
            answer="A fixed synthetic fact.",
            notes=[{"path": "Topics/Synthetic.md", "content": copied_note.read_text()}],
            rounds=1,
            errors=[],
            usage={},
        )

    monkeypatch.setattr(
        retrieval,
        "load_search_executor",
        lambda _executor: (fake_search, {"model_name": "synthetic-model"}),
    )

    exit_code = await retrieval._run(_benchmark_args(source, questions, output))

    assert exit_code == 0
    assert retrieval.snapshot_tree(source) == source_before
    assert len(received_vaults) == 1
    assert not received_vaults[0].exists()
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["vault"]["source"]["unchanged"] is True
    assert manifest["vault"]["copy"]["unchanged"] is True
    assert manifest["vault"]["copy"]["ephemeral"] is True
    assert manifest["summary"]["source_vault_unchanged"] is True
    assert manifest["summary"]["copy_vault_unchanged"] is True


@pytest.mark.asyncio
async def test_benchmark_aborts_when_isolated_copy_changes(tmp_path, monkeypatch):
    source = tmp_path / "source-vault"
    (source / "Topics").mkdir(parents=True)
    source_before = retrieval.snapshot_tree(source)
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps([{"id": "q1", "question": "Do not change the vault."}]),
        encoding="utf-8",
    )
    output = tmp_path / "private-output" / "manifest.json"

    async def mutating_search(_query, vault, **_kwargs):
        (vault / "Topics" / "Unexpected.md").write_text("changed", encoding="utf-8")
        return SimpleNamespace(
            answer="Changed it.", notes=[], rounds=1, errors=[], usage={}
        )

    monkeypatch.setattr(
        retrieval,
        "load_search_executor",
        lambda _executor: (mutating_search, {"model_name": "synthetic-model"}),
    )

    exit_code = await retrieval._run(_benchmark_args(source, questions, output))

    assert exit_code == 1
    assert retrieval.snapshot_tree(source) == source_before
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["abort_reasons"] == ["isolated vault copy changed"]
    assert manifest["vault"]["source"]["unchanged"] is True
    assert manifest["vault"]["copy"]["unchanged"] is False


@pytest.mark.asyncio
async def test_benchmark_invalidates_run_when_source_changes(tmp_path, monkeypatch):
    source = tmp_path / "source-vault"
    note = source / "Topics" / "Synthetic.md"
    note.parent.mkdir(parents=True)
    note.write_text("original", encoding="utf-8")
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps([{"id": "q1", "question": "Is the source fixed?"}]),
        encoding="utf-8",
    )
    output = tmp_path / "private-output" / "manifest.json"

    async def externally_mutating_search(_query, _vault, **_kwargs):
        note.write_text("changed externally", encoding="utf-8")
        return SimpleNamespace(
            answer="The copy stayed fixed.", notes=[], rounds=1, errors=[], usage={}
        )

    monkeypatch.setattr(
        retrieval,
        "load_search_executor",
        lambda _executor: (
            externally_mutating_search,
            {"model_name": "synthetic-model"},
        ),
    )

    exit_code = await retrieval._run(_benchmark_args(source, questions, output))

    assert exit_code == 1
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["abort_reasons"] == ["source vault changed"]
    assert manifest["runs"][0]["source_vault_unchanged"] is False
    assert manifest["runs"][0]["ok"] is False
    assert manifest["vault"]["source"]["unchanged"] is False
    assert manifest["vault"]["copy"]["unchanged"] is True
