#!/usr/bin/env python3
"""Run Chronicle's direct or Pi retrieval agent over a private copy of a fixed vault."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Mapping

MANIFEST_KIND = "chronicle-memory-retrieval-benchmark"
MANIFEST_SCHEMA_VERSION = 3
STOPPED_ANSWER = "(search stopped at max rounds)"
PI_FAILED_ANSWER = "(Pi search failed before completing)"
NON_ANSWERS = frozenset({STOPPED_ANSWER, PI_FAILED_ANSWER})


class RetrievalInputError(ValueError):
    """The vault, question set, or output target is invalid."""


@dataclass(frozen=True)
class Question:
    question_id: str
    question: str
    vault_summary: str
    source_position: int


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", choices=("direct", "pi"), required=True)
    parser.add_argument(
        "--vault",
        type=Path,
        required=True,
        help="fixed source vault copied for the run",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        required=True,
        help="UTF-8 .json or .jsonl question set",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new manifest JSON path outside the vault",
    )
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument(
        "--vault-summary",
        default="",
        help="default learned vault context; a question's vault_summary overrides it",
    )
    args = parser.parse_args(argv)
    if args.max_rounds <= 0:
        parser.error("--max-rounds must be positive")
    return args


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _question_from_value(
    value: Any, position: int, default_vault_summary: str
) -> Question:
    if not isinstance(value, dict):
        raise RetrievalInputError(f"question {position}: value must be an object")
    question_id = value.get("id")
    question = value.get("question")
    vault_summary = value.get("vault_summary", default_vault_summary)
    if not isinstance(question_id, str) or not question_id:
        raise RetrievalInputError(
            f"question {position}: 'id' must be a non-empty string"
        )
    if not isinstance(question, str) or not question:
        raise RetrievalInputError(
            f"question {position}: 'question' must be a non-empty string"
        )
    if not isinstance(vault_summary, str):
        raise RetrievalInputError(
            f"question {position}: 'vault_summary' must be a string"
        )
    return Question(question_id, question, vault_summary, position)


def load_questions(
    path: Path, default_vault_summary: str = ""
) -> tuple[list[Question], str]:
    path = path.resolve()
    if not path.is_file():
        raise RetrievalInputError(f"question set does not exist: {path}")
    raw_bytes = path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RetrievalInputError(f"question set is not UTF-8: {exc}") from exc

    values: list[Any]
    if path.suffix.casefold() in {".jsonl", ".ndjson"}:
        values = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RetrievalInputError(
                    f"line {line_number}: invalid JSON: {exc}"
                ) from exc
    else:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RetrievalInputError(f"invalid JSON: {exc}") from exc
        if isinstance(document, dict):
            values = document.get("questions")
            if not isinstance(values, list):
                raise RetrievalInputError("JSON object must contain a 'questions' list")
        elif isinstance(document, list):
            values = document
        else:
            raise RetrievalInputError(
                "JSON root must be a list or an object with 'questions'"
            )

    if not values:
        raise RetrievalInputError("question set has no questions")
    questions = [
        _question_from_value(value, position, default_vault_summary)
        for position, value in enumerate(values, 1)
    ]
    ids = [question.question_id for question in questions]
    if len(ids) != len(set(ids)):
        raise RetrievalInputError("question ids must be unique")
    return questions, _sha256_bytes(raw_bytes)


def snapshot_tree(root: Path) -> dict[str, dict[str, Any]]:
    """Fingerprint files, symlinks, and directories without changing the vault."""
    snapshot: dict[str, dict[str, Any]] = {
        ".": {
            "kind": "directory",
            "mode": f"{stat.S_IMODE(root.lstat().st_mode):04o}",
        }
    }
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        mode = f"{stat.S_IMODE(path.lstat().st_mode):04o}"
        if path.is_symlink():
            snapshot[rel] = {
                "kind": "symlink",
                "mode": mode,
                "target_sha256": _sha256_text(str(path.readlink())),
            }
        elif path.is_dir():
            snapshot[rel] = {"kind": "directory", "mode": mode}
        elif path.is_file():
            snapshot[rel] = {
                "kind": "file",
                "mode": mode,
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
    return snapshot


def snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    return _sha256_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))


def snapshot_diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, list[str]]:
    return {
        "created": sorted(path for path in after if path not in before),
        "modified": sorted(
            path for path in after if path in before and before[path] != after[path]
        ),
        "removed": sorted(path for path in before if path not in after),
    }


def _require_git_ignored(path: Path) -> None:
    """Refuse private evidence inside a worktree unless an existing rule ignores it."""
    worktree = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if worktree.returncode != 0:
        return
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", str(path)],
        cwd=path.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ignored.returncode != 0:
        raise RetrievalInputError(
            "manifest output is inside a Git worktree but is not ignored by Git; "
            "choose a path outside the checkout or add an explicit ignore rule"
        )


def prepare_paths(vault: Path, output: Path) -> tuple[Path, Path]:
    vault = vault.resolve()
    if not vault.is_dir():
        raise RetrievalInputError(f"vault is not an existing directory: {vault}")
    output = output.resolve()
    if output == vault or output.is_relative_to(vault):
        raise RetrievalInputError("manifest output must be outside the fixed vault")
    if output.exists():
        raise RetrievalInputError(f"refusing existing manifest output: {output}")
    output_parent_existed = output.parent.exists()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not output.parent.is_dir():
        raise RetrievalInputError(
            f"manifest output parent is not a directory: {output.parent}"
        )
    if not output_parent_existed:
        output.parent.chmod(0o700)
    parent_mode = stat.S_IMODE(output.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise RetrievalInputError(
            "manifest output parent must be private (mode 0700); choose a new "
            f"directory or chmod it first: {output.parent} has mode {parent_mode:04o}"
        )
    _require_git_ignored(output)
    return vault, output


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _harden_private_tree(root: Path) -> None:
    """Make a copied vault private without following any copied symlinks."""
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def _copy_private_vault(source: Path, destination: Path) -> None:
    """Copy a source vault without allowing the benchmark to operate on it directly."""
    shutil.copytree(source, destination, symlinks=True)
    _harden_private_tree(destination)


def _git_metadata(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    return {
        "commit": run("rev-parse", "HEAD") or None,
        "dirty": bool(run("status", "--porcelain")),
    }


def _runtime_metadata(executor: str, pi_module: Any = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"operation": "memory_search"}
    if executor == "direct":
        try:
            # Soft dependency: the except below covers hosts without it.
            from backend.model_registry import get_models_registry

            registry = get_models_registry()
            operation = (
                registry.get_llm_operation("memory_search") if registry else None
            )
            if operation is not None:
                model_def = operation.model_def
                metadata.update(
                    {
                        "model_name": str(operation.model_name),
                        "model_provider": str(model_def.model_provider),
                        "registry_model": str(model_def.name),
                        "thinking_model": bool(model_def.thinking),
                        "operation_params": operation.to_api_params(),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - provenance must not block a run
            metadata["registry_error"] = f"{type(exc).__name__}: {exc}"
    elif executor == "pi" and pi_module is not None:
        available, detail = pi_module.pi_executor_available()
        metadata.update(
            {"executor_available": bool(available), "executor_detail": str(detail)}
        )
        try:
            # Soft dependency: the except below covers hosts without it.
            from backend.model_registry import get_models_registry

            registry = get_models_registry()
            if registry is None:
                raise RuntimeError("Chronicle model registry is unavailable")
            settings = pi_module._pi_settings(registry)
            resolved = pi_module._resolve_pi_config("memory_search")
            metadata.update(
                {
                    # Upstream API identity Pi sends versus Chronicle registry override.
                    "model_name": str(resolved.model),
                    "model_provider": str(resolved.provider),
                    "pi_model_override": str(settings.get("model") or "") or None,
                    "pi_context_window": resolved.context_window,
                    "pi_max_tokens": resolved.max_tokens,
                    "pi_temperature": resolved.temperature,
                    "pi_thinking": str(resolved.thinking),
                    "pi_reasoning": bool(resolved.reasoning),
                    "pi_timeout_seconds": resolved.timeout_seconds,
                    "pi_compat": dict(resolved.compat),
                }
            )
        except Exception as exc:  # noqa: BLE001 - agent result captures hard failures
            metadata["resolution_error"] = f"{type(exc).__name__}: {exc}"
    return metadata


def load_search_executor(
    executor: str,
) -> tuple[Callable[..., Awaitable[Any]], dict[str, Any]]:
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend / "src"))
    if executor == "direct":
        # Imported here: backend/src only joins sys.path at runtime (above).
        from backend.services.memory.agent.memory_agent import search_vault

        return search_vault, _runtime_metadata(executor)
    # Imported here: backend/src only joins sys.path at runtime (above).
    from backend.services.memory.agent import pi_agent

    return pi_agent.search_vault_with_pi, _runtime_metadata(executor, pi_agent)


def _normalized_reference_path(value: Any) -> str | None:
    """Mirror the read tool's harmless path normalization for manifest references."""
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip().replace("\\", "/").lstrip("/")
    if not raw.endswith(".md"):
        raw += ".md"
    path = PurePosixPath(raw)
    if ".." in path.parts or len(path.parts) not in (1, 2):
        return None
    parts = [part.strip() for part in path.parts]
    if not all(parts) or not parts[-1][:-3].strip():
        return None
    parts[-1] = parts[-1][:-3].strip() + ".md"
    return PurePosixPath(*parts).as_posix()


def _references(notes: Any, vault: Path) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for note in notes or []:
        if not isinstance(note, Mapping):
            references.append({"path": str(note), "valid_path": False})
            continue
        reported_path = note.get("path")
        path = _normalized_reference_path(reported_path)
        content = note.get("content")
        entry: dict[str, Any] = {
            "path": path or str(reported_path or ""),
            "valid_path": path is not None,
        }
        if path is not None and path != reported_path:
            entry["reported_path"] = str(reported_path)
        if isinstance(content, str):
            entry["content_sha256"] = _sha256_text(content)
            entry["content_chars"] = len(content)
        if entry["valid_path"]:
            entry["exists_in_vault"] = (vault / path).is_file()
        references.append(entry)
    return references


def _usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    }


async def run_question(
    search: Callable[..., Awaitable[Any]],
    vault: Path,
    question: Question,
    *,
    max_rounds: int,
) -> dict[str, Any]:
    before = snapshot_tree(vault)
    started = time.perf_counter()
    result = None
    exception: Exception | None = None
    try:
        result = await search(
            question.question,
            vault,
            operation="memory_search",
            max_rounds=max_rounds,
            vault_summary=question.vault_summary,
        )
    except Exception as exc:  # noqa: BLE001 - preserve failure details in the manifest
        exception = exc
    elapsed = time.perf_counter() - started
    after = snapshot_tree(vault)
    diff = snapshot_diff(before, after)
    vault_unchanged = before == after

    if exception is not None:
        answer = ""
        notes: Any = []
        rounds = 0
        errors = [f"{type(exception).__name__}: {exception}"]
        warnings: list[str] = []
        usage: dict[str, int | float] = {}
        returned = False
    else:
        answer = str(getattr(result, "answer", "") or "")
        notes = getattr(result, "notes", []) or []
        rounds = int(getattr(result, "rounds", 0) or 0)
        errors = [str(error) for error in (getattr(result, "errors", []) or [])]
        warnings = [str(warning) for warning in (getattr(result, "warnings", []) or [])]
        usage = _usage(getattr(result, "usage", {}) or {})
        returned = True
    answered = bool(answer.strip() and answer.strip() not in NON_ANSWERS)
    references = _references(notes, vault)
    invalid_references = sum(
        not reference.get("valid_path") or not reference.get("exists_in_vault", False)
        for reference in references
    )
    return {
        "id": question.question_id,
        "question": question.question,
        "question_sha256": _sha256_text(question.question),
        "vault_summary_sha256": _sha256_text(question.vault_summary),
        "vault_summary_chars": len(question.vault_summary),
        "source_position": question.source_position,
        "latency_seconds": round(elapsed, 6),
        "returned": returned,
        "answered": answered,
        "ok": bool(
            returned
            and answered
            and not errors
            and invalid_references == 0
            and vault_unchanged
        ),
        "answer": answer,
        "referenced_notes": references,
        "invalid_reference_count": invalid_references,
        "rounds": rounds,
        "errors": errors,
        "warnings": warnings,
        "usage": usage,
        "vault_unchanged": vault_unchanged,
        "vault_diff": diff,
        "vault_fingerprint_before": snapshot_fingerprint(before),
        "vault_fingerprint_after": snapshot_fingerprint(after),
    }


async def _run(args: argparse.Namespace) -> int:
    source_vault, output = prepare_paths(args.vault, args.output)
    questions, questions_sha256 = load_questions(args.questions, args.vault_summary)
    search, runtime = load_search_executor(args.executor)
    source_initial_snapshot = snapshot_tree(source_vault)
    source_initial_fingerprint = snapshot_fingerprint(source_initial_snapshot)
    backend = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory(
        dir=output.parent, prefix=".chronicle-memory-retrieval-"
    ) as temporary_name:
        private_workspace = Path(temporary_name)
        private_workspace.chmod(0o700)
        benchmark_vault = private_workspace / "vault"
        _copy_private_vault(source_vault, benchmark_vault)

        source_after_copy = snapshot_tree(source_vault)
        if source_after_copy != source_initial_snapshot:
            raise RetrievalInputError(
                "source vault changed while creating the isolated benchmark copy"
            )
        copy_initial_snapshot = snapshot_tree(benchmark_vault)
        copy_initial_fingerprint = snapshot_fingerprint(copy_initial_snapshot)

        manifest: dict[str, Any] = {
            "kind": MANIFEST_KIND,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "executor": args.executor,
            "runtime": runtime,
            "settings": {"max_rounds": args.max_rounds},
            "vault": {
                "source": {
                    "path": str(source_vault),
                    "initial_fingerprint": source_initial_fingerprint,
                    "initial_entry_count": len(source_initial_snapshot),
                    "final_fingerprint": None,
                    "unchanged": None,
                },
                "copy": {
                    "ephemeral": True,
                    "initial_fingerprint": copy_initial_fingerprint,
                    "initial_entry_count": len(copy_initial_snapshot),
                    "final_fingerprint": None,
                    "unchanged": None,
                },
            },
            "questions": {
                "path": str(args.questions.resolve()),
                "sha256": questions_sha256,
                "ids": [question.question_id for question in questions],
            },
            "environment": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "git": _git_metadata(backend),
            },
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "aborted_after_question": None,
            "abort_reasons": [],
            "runs": [],
            "summary": None,
        }
        _atomic_write_json(output, manifest)

        for index, question in enumerate(questions, 1):
            print(
                f"[{index}/{len(questions)}] {args.executor}: {question.question_id}",
                flush=True,
            )
            run = await run_question(
                search, benchmark_vault, question, max_rounds=args.max_rounds
            )
            source_after_question = snapshot_tree(source_vault)
            source_unchanged = source_after_question == source_initial_snapshot
            run["source_vault_unchanged"] = source_unchanged
            run["ok"] = bool(run["ok"] and source_unchanged)
            manifest["runs"].append(run)
            abort_reasons: list[str] = []
            if not run["vault_unchanged"]:
                abort_reasons.append("isolated vault copy changed")
            if not source_unchanged:
                abort_reasons.append("source vault changed")
            if abort_reasons:
                manifest["aborted_after_question"] = question.question_id
                manifest["abort_reasons"] = abort_reasons
                _atomic_write_json(output, manifest)
                break
            _atomic_write_json(output, manifest)

        copy_final_snapshot = snapshot_tree(benchmark_vault)
        copy_final_fingerprint = snapshot_fingerprint(copy_final_snapshot)
        copy_unchanged = copy_final_snapshot == copy_initial_snapshot
        source_final_snapshot = snapshot_tree(source_vault)
        source_final_fingerprint = snapshot_fingerprint(source_final_snapshot)
        source_unchanged = source_final_snapshot == source_initial_snapshot
        manifest["vault"]["copy"]["final_fingerprint"] = copy_final_fingerprint
        manifest["vault"]["copy"]["unchanged"] = copy_unchanged
        manifest["vault"]["source"]["final_fingerprint"] = source_final_fingerprint
        manifest["vault"]["source"]["unchanged"] = source_unchanged
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["summary"] = {
            "question_count": len(questions),
            "completed_count": len(manifest["runs"]),
            "ok_count": sum(bool(run["ok"]) for run in manifest["runs"]),
            "answered_count": sum(bool(run["answered"]) for run in manifest["runs"]),
            "error_case_count": sum(bool(run["errors"]) for run in manifest["runs"]),
            "warning_case_count": sum(
                bool(run["warnings"]) for run in manifest["runs"]
            ),
            "invalid_reference_count": sum(
                int(run["invalid_reference_count"]) for run in manifest["runs"]
            ),
            "latency_seconds": round(
                sum(float(run["latency_seconds"]) for run in manifest["runs"]), 6
            ),
            "source_vault_unchanged": source_unchanged,
            "copy_vault_unchanged": copy_unchanged,
            "semantic_quality_scored": False,
        }
        _atomic_write_json(output, manifest)

    print(f"manifest: {output}")
    return int(
        len(manifest["runs"]) != len(questions)
        or manifest["summary"]["ok_count"] != len(questions)
        or not source_unchanged
        or not copy_unchanged
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(_parse_args(argv)))
    except RetrievalInputError as exc:
        print(f"retrieval benchmark input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
