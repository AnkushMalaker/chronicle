"""Focused tests for the isolated memory-executor benchmark scripts."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import model_registry

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "src"))

import evaluate_memory_executor as evaluate
import score_memory_executor as score


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _args(dataset: Path, output: Path, *prefixes: str):
    argv = [
        "--executor",
        "direct",
        "--dataset",
        str(dataset),
        "--output",
        str(output),
    ]
    for prefix in prefixes:
        argv.extend(("--source-id-prefix", prefix))
    return evaluate._parse_args(argv)


def test_jsonl_selection_preserves_exact_inputs_and_prefix_order(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    transcript = "  Speaker 0: Keep leading and trailing whitespace.  \n"
    rows = [
        {
            "conversation_id": "alpha-001",
            "created_at": "2026-08-01T10:00:00+00:00",
            "transcript": "Speaker 0: Alpha",
            "duration_s": 60,
        },
        {
            "conversation_id": "beta-001",
            "created_at": "2026-08-02T10:00:00+00:00",
            "transcript": transcript,
            "guidance": "  Preserve this guidance exactly.  ",
            "duration_s": 90,
            "title": "Synthetic case",
        },
    ]
    dataset.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    cases, digest = evaluate.load_cases(
        dataset, _args(dataset, tmp_path / "output", "beta", "alpha")
    )

    assert [case.source_id for case in cases] == ["beta-001", "alpha-001"]
    assert cases[0].transcript == transcript
    assert cases[0].guidance == "  Preserve this guidance exactly.  "
    assert cases[0].date == "2026-08-02T10:00:00+00:00"
    assert cases[0].duration_minutes == 1.5
    assert digest == evaluate._file_sha256(dataset)
    serialized_input = json.dumps(cases[0].input_record())
    assert transcript not in serialized_input
    assert cases[0].guidance not in serialized_input


def test_jsonl_selection_rejects_ambiguous_prefix(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    rows = [
        {
            "conversation_id": source_id,
            "created_at": "2026-08-01T10:00:00+00:00",
            "transcript": "Speaker 0: Synthetic transcript",
        }
        for source_id in ("same-001", "same-002")
    ]
    dataset.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    with pytest.raises(evaluate.BenchmarkInputError, match="matched 2 cases"):
        evaluate.load_cases(dataset, _args(dataset, tmp_path / "output", "same"))


def test_input_fingerprint_ignores_dataset_position():
    common = {
        "source_id": "case-001",
        "transcript": "Speaker 0: Exact source.",
        "date": "2026-08-01T10:00:00+00:00",
        "guidance": "Exact guidance.",
        "duration_seconds": 60.0,
        "duration_minutes": 1.0,
        "title": "Exact title",
    }
    first = evaluate.BenchmarkCase(dataset_line=1, **common).input_record()
    moved = evaluate.BenchmarkCase(dataset_line=99, **common).input_record()

    assert first["dataset_line"] == 1
    assert moved["dataset_line"] == 99
    assert first["fingerprint_sha256"] == moved["fingerprint_sha256"]


def test_output_and_checkpoint_artifacts_are_private_and_self_ignoring(tmp_path):
    output, vault = evaluate._prepare_output(tmp_path / "run")
    manifest = output / "manifest.json"

    previous_umask = os.umask(0)
    try:
        evaluate._atomic_write_json(manifest, {"private": True})
    finally:
        os.umask(previous_umask)

    assert _mode(output) == 0o700
    assert _mode(vault) == 0o700
    assert _mode(output / ".gitignore") == 0o600
    assert (output / ".gitignore").read_text(encoding="utf-8") == "*\n!.gitignore\n"
    assert _mode(manifest) == 0o600


def test_private_tree_normalizer_locks_down_agent_created_artifacts(tmp_path):
    output = tmp_path / "run"
    nested = output / "vault" / "People"
    nested.mkdir(parents=True)
    note = nested / "Synthetic.md"
    note.write_text("private source facts", encoding="utf-8")
    output.chmod(0o775)
    (output / "vault").chmod(0o775)
    nested.chmod(0o775)
    note.chmod(0o664)

    evaluate._make_tree_private(output)

    assert _mode(output) == 0o700
    assert _mode(output / "vault") == 0o700
    assert _mode(nested) == 0o700
    assert _mode(note) == 0o600


def _conversation(case: evaluate.BenchmarkCase) -> str:
    return f"""---
categories:
  - "[[Conversations]]"
conversation_id: "{case.source_id}"
date: "{case.date}"
people: []
topics: []
duration_minutes: {case.duration_minutes}
---
## Synthetic benchmark conversation

### Summary
The speakers made a concrete, synthetic benchmark decision.

### Key Facts
- The synthetic decision is suitable for structural testing.

### Action Items
- [ ] Verify the benchmark artifact.
"""


def test_vault_scanner_checks_canonical_metadata_and_forbidden_people(tmp_path):
    case = evaluate.BenchmarkCase(
        source_id="case-001",
        dataset_line=1,
        transcript="Speaker 0: Synthetic transcript",
        date="2026-08-01T10:00:00+00:00",
        guidance="",
        duration_seconds=90,
        duration_minutes=1.5,
        title="Synthetic benchmark conversation",
    )
    note = tmp_path / "Conversations" / "case-001.md"
    note.parent.mkdir()
    note.write_text(_conversation(case), encoding="utf-8")

    clean = evaluate.scan_vault(tmp_path, {case.source_id: case}, {})
    assert clean["ok"] is True

    forbidden = tmp_path / "People" / "Unknown Speaker 2.md"
    forbidden.parent.mkdir()
    forbidden.write_text(
        """---
categories: ["[[People]]"]
---
## About
- Synthetic placeholder.

## Conversations
![[Conversations.base#Person]]

## Mentions
- 2026-08-01 — Synthetic placeholder.
""",
        encoding="utf-8",
    )
    dirty = evaluate.scan_vault(tmp_path, {case.source_id: case}, {})
    assert dirty["ok"] is False
    assert any(issue["code"] == "forbidden_person_note" for issue in dirty["issues"])


def test_vault_scanner_accepts_canonical_duration_precision(tmp_path):
    duration_minutes = 1928.18 / 60
    case = evaluate.BenchmarkCase(
        source_id="case-precision",
        dataset_line=1,
        transcript="Speaker 0: Synthetic transcript",
        date="2026-08-01T10:00:00+00:00",
        guidance="",
        duration_seconds=1928.18,
        duration_minutes=duration_minutes,
        title="Synthetic benchmark conversation",
    )
    note = tmp_path / "Conversations" / "case-precision.md"
    note.parent.mkdir()
    note.write_text(
        _conversation(case).replace(
            f"duration_minutes: {duration_minutes}",
            f"duration_minutes: {duration_minutes:g}",
        ),
        encoding="utf-8",
    )

    result = evaluate.scan_vault(tmp_path, {case.source_id: case}, {})

    assert result["ok"] is True


@pytest.mark.parametrize(
    ("folder", "sections", "issue_code"),
    [
        (
            "People",
            "## About\n- Person.\n\n## Conversations\n\n## Mentions\n- Mention.",
            "missing_person_embed",
        ),
        (
            "Topics",
            "## About\n- Topic.\n\n## Conversations\n",
            "missing_topic_embed",
        ),
    ],
)
def test_vault_scanner_requires_exact_aggregation_embed(
    tmp_path, folder, sections, issue_code
):
    note = tmp_path / folder / "Synthetic.md"
    note.parent.mkdir()
    note.write_text(
        f'---\ncategories: ["[[{folder}]]"]\n---\n{sections}\n', encoding="utf-8"
    )

    result = evaluate.scan_vault(tmp_path, {}, {})

    assert result["ok"] is False
    assert any(issue["code"] == issue_code for issue in result["issues"])


@pytest.mark.asyncio
async def test_run_case_passes_exact_inputs_and_records_deterministic_fallback(
    tmp_path,
):
    case = evaluate.BenchmarkCase(
        source_id="case-001",
        dataset_line=1,
        transcript="  Speaker 0: A deliberately short synthetic source.  ",
        date="2026-08-01T10:00:00+00:00",
        guidance="  exact guidance  ",
        duration_seconds=12,
        duration_minutes=0.2,
        title="Synthetic source",
    )
    received = {}

    class MissingNoteAgent:
        def __init__(self, vault_root):
            self.vault_root = vault_root

        async def run(self, transcript, source_id, **kwargs):
            received.update(
                {"transcript": transcript, "source_id": source_id, **kwargs}
            )
            return SimpleNamespace(
                rounds=1,
                tool_calls=0,
                touched=[],
                removed=[],
                errors=[],
                usage={"input_tokens": 10, "output_tokens": 2},
                truncated=False,
                stalled=False,
                summary="No note written.",
            )

    run = await evaluate._run_case(
        MissingNoteAgent,
        tmp_path,
        case,
        {case.source_id: case},
        {},
    )

    assert received["transcript"] == case.transcript
    assert received["date"] == case.date
    assert received["guidance"] == case.guidance
    assert run["conversation_note"]["primary_canonical"] is False
    assert run["conversation_note"]["fallback_written"] is True
    assert run["conversation_note"]["final_canonical"] is True
    assert run["completed"] is True
    assert run["ok"] is False
    assert "summary" not in run["result"]
    assert run["result"]["summary_chars"] == len("No note written.")
    assert (tmp_path / "Conversations" / "case-001.md").is_file()


@pytest.mark.asyncio
async def test_run_case_does_not_recount_preexisting_invariant_issue(tmp_path):
    prior = evaluate.BenchmarkCase(
        source_id="prior-case",
        dataset_line=1,
        transcript="Speaker 0: Prior synthetic source.",
        date="2026-08-01T10:00:00+00:00",
        guidance="",
        duration_seconds=60,
        duration_minutes=1.0,
        title="Prior synthetic source",
    )
    current = evaluate.BenchmarkCase(
        source_id="current-case",
        dataset_line=2,
        transcript="Speaker 0: Current synthetic source.",
        date="2026-08-02T10:00:00+00:00",
        guidance="",
        duration_seconds=60,
        duration_minutes=1.0,
        title="Current synthetic source",
    )
    conversations = tmp_path / "Conversations"
    conversations.mkdir()
    (conversations / "prior-case.md").write_text(_conversation(prior), encoding="utf-8")
    topics = tmp_path / "Topics"
    topics.mkdir()
    (topics / "Malformed.md").write_text(
        '---\ncategories: ["[[Topics]]"]\n---\n## About\n- Existing issue.\n\n'
        "![[Conversations.base#Topic]]\n",
        encoding="utf-8",
    )

    class ValidAgent:
        def __init__(self, vault_root):
            self.vault_root = vault_root

        async def run(self, _transcript, source_id, **_kwargs):
            (self.vault_root / "Conversations" / f"{source_id}.md").write_text(
                _conversation(current), encoding="utf-8"
            )
            return SimpleNamespace(
                rounds=1,
                tool_calls=1,
                touched=[f"Conversations/{source_id}.md"],
                removed=[],
                errors=[],
                usage={},
                truncated=False,
                stalled=False,
                summary="Wrote the current note.",
            )

    run = await evaluate._run_case(
        ValidAgent,
        tmp_path,
        current,
        {prior.source_id: prior, current.source_id: current},
        {},
    )

    assert run["vault_invariants"]["issue_count"] == 1
    assert run["vault_invariants"]["introduced_issue_count"] == 0
    assert run["vault_invariants"]["resolved_issue_count"] == 0
    assert run["ok"] is True


def test_pi_runtime_metadata_uses_resolved_override_and_effective_limits(monkeypatch):
    ambient_operation = SimpleNamespace(
        model_name="ambient-memory-write-model",
        model_provider="ambient-provider",
    )
    registry = SimpleNamespace(
        get_llm_operation=lambda _operation: ambient_operation,
    )
    monkeypatch.setattr(model_registry, "get_models_registry", lambda: registry)
    resolved = SimpleNamespace(
        model="unsloth/Qwen3.6-27B-GGUF:Q4_K_M",
        provider="chronicle-llamacpp",
        context_window=65536,
        max_tokens=4096,
        temperature=0.17,
        thinking="off",
        reasoning=True,
        timeout_seconds=900,
        compat={"thinkingFormat": "qwen-chat-template"},
    )
    pi_agent = SimpleNamespace(
        pi_executor_available=lambda: (True, "/usr/bin/pi"),
        _pi_settings=lambda _registry: {"model": "qwen36-llm"},
        _resolve_pi_config=lambda _operation: resolved,
    )

    metadata = evaluate._safe_runtime_metadata("pi", {"pi_agent": pi_agent})

    assert metadata["model_name"] == "unsloth/Qwen3.6-27B-GGUF:Q4_K_M"
    assert metadata["model_provider"] == "chronicle-llamacpp"
    assert metadata["pi_model_override"] == "qwen36-llm"
    assert metadata["pi_context_window"] == 65536
    assert metadata["pi_max_tokens"] == 4096
    assert metadata["pi_temperature"] == 0.17
    assert metadata["pi_thinking"] == "off"
    assert metadata["pi_reasoning"] is True


def test_codex_runtime_metadata_uses_actual_codex_model(monkeypatch):
    ambient_operation = SimpleNamespace(
        model_name="ambient-memory-write-model",
        model_provider="ambient-provider",
    )
    registry = SimpleNamespace(
        get_llm_operation=lambda _operation: ambient_operation,
    )
    monkeypatch.setattr(model_registry, "get_models_registry", lambda: registry)
    settings = {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "low",
        "sandbox_mode": "danger-full-access",
        "timeout_seconds": 900,
        "max_used_percent": None,
        "limit_id": "",
    }
    codex_agent = SimpleNamespace(
        codex_executor_available=lambda: (True, "/usr/bin/codex"),
        _validated_codex_settings=lambda: settings,
    )

    metadata = evaluate._safe_runtime_metadata("codex", {"codex_agent": codex_agent})

    assert metadata["model_name"] == "gpt-5.6-terra"
    assert metadata["model_provider"] == "openai_codex_cli"
    assert metadata["codex_model"] == "gpt-5.6-terra"


def _manifest(executor: str, latency: float, fallback: bool, fingerprint: str):
    return {
        "kind": score.MANIFEST_KIND,
        "schema_version": score.MANIFEST_SCHEMA_VERSION,
        "executor": executor,
        "finished_at": "2026-08-01T10:00:00+00:00",
        "runtime": {"model_name": "synthetic-model"},
        "runs": [
            {
                "source_id": "case-001",
                "input": {"fingerprint_sha256": fingerprint},
                "ok": not fallback,
                "completed": True,
                "agent_completed": True,
                "latency_seconds": latency,
                "total_elapsed_seconds": latency + 0.1,
                "conversation_note": {
                    "primary_canonical": not fallback,
                    "fallback_written": fallback,
                },
                "result": {
                    "rounds": 2,
                    "tool_calls": 3,
                    "errors": [],
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "truncated": False,
                    "stalled": False,
                },
                "vault_invariants": {"issue_count": 0},
            }
        ],
        "vault": {"invariants": {"ok": True, "issue_count": 0, "issues": []}},
    }


def test_scorer_compares_only_identical_input_sequences():
    baseline = _manifest("direct", 10.0, False, "same")
    candidate = _manifest("pi", 5.0, True, "same")

    report = score.build_report([baseline, candidate])

    assert report["semantic_quality_scored"] is False
    assert report["summaries"][0]["tokens"]["total"] == 120
    assert report["comparisons"][0]["ratio"]["median_latency"] == 0.5
    assert report["comparisons"][0]["delta"]["fallback_rate_points"] == 100

    candidate["runs"][0]["input"]["fingerprint_sha256"] = "different"
    with pytest.raises(score.ManifestError, match="fingerprints differ"):
        score.build_report([baseline, candidate])


def test_scorer_labels_codex_with_actual_executor_model():
    manifest = _manifest("codex", 10.0, False, "same")
    manifest["runtime"] = {
        "model_name": "ambient-memory-write-model",
        "codex_model": "gpt-5.6-terra",
    }

    summary = score.summarize_manifest(manifest)

    assert summary["model"] == "gpt-5.6-terra"
