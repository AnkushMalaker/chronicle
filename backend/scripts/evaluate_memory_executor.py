#!/usr/bin/env python3
"""Replay transcript cases into an isolated Chronicle vault.

The benchmark intentionally bypasses MongoDB, Redis, queues, and the live vault.  It
runs one selected memory writer per case, applies Chronicle's deterministic conversation
note canonicalizer, and records a source-preserving fallback when the primary writer did
not produce a valid note.  Recovery agents are not invoked: a run labelled ``pi`` or
``codex`` therefore measures that executor rather than a mixture of executors.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

MANIFEST_KIND = "chronicle-memory-executor-benchmark"
MANIFEST_SCHEMA_VERSION = 1

_H2_RE = re.compile(r"^##(?!#)\s+(.+?)\s*$", re.MULTILINE)
_H3_RE = re.compile(r"^###(?!#)\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\s*$", re.MULTILINE)
_UNKNOWN_PERSON_RE = re.compile(r"^unknown[ _-]*speaker(?:[ _-]*\d+)?$", re.IGNORECASE)
_UNKNOWN_LINK_RE = re.compile(
    r"\[\[unknown\s+speaker(?:\s+\d+)?(?:\|[^]]+)?]]", re.IGNORECASE
)
_PLACEHOLDERS = {"", "-", "none", "n/a", "unknown", "untitled", "[ ]", "- [ ]"}
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_ARTIFACT_GITIGNORE = "*\n!.gitignore\n"


class BenchmarkInputError(ValueError):
    """The requested dataset selection is ambiguous or incomplete."""


@dataclass(frozen=True)
class BenchmarkCase:
    source_id: str
    dataset_line: int
    transcript: str
    date: str
    guidance: str
    duration_seconds: float | None
    duration_minutes: float | None
    title: str | None

    def input_record(self) -> dict[str, Any]:
        """Return a non-plaintext fingerprint of the exact agent inputs."""
        title = self.title or ""
        record = {
            "dataset_line": self.dataset_line,
            "transcript_sha256": _text_sha256(self.transcript),
            "transcript_chars": len(self.transcript),
            "date": self.date,
            "guidance_sha256": _text_sha256(self.guidance),
            "guidance_chars": len(self.guidance),
            "duration_seconds": self.duration_seconds,
            "duration_minutes_passed": self.duration_minutes,
            "title_sha256": _text_sha256(title),
            "title_chars": len(title),
            "title_present": self.title is not None,
        }
        # Dataset position is useful audit context but is not passed to the agent.
        # Keep comparisons stable when unrelated JSONL rows are reordered.
        fingerprint_payload = {
            "source_id": self.source_id,
            **{key: value for key, value in record.items() if key != "dataset_line"},
        }
        record["fingerprint_sha256"] = _text_sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))
        )
        return record


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", choices=("direct", "pi", "codex"), required=True)
    parser.add_argument(
        "--dataset", type=Path, required=True, help="UTF-8 JSONL dataset"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new or empty run directory; receives vault/ and manifest.json",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--source-id-prefix",
        action="append",
        dest="source_id_prefixes",
        help="unique source-id prefix; repeat to define the cumulative replay order",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="replay every dataset row in file order",
    )
    parser.add_argument("--source-id-field", default="conversation_id")
    parser.add_argument("--transcript-field", default="transcript")
    parser.add_argument("--date-field", default="created_at")
    parser.add_argument("--guidance-field", default="guidance")
    parser.add_argument("--duration-seconds-field", default="duration_s")
    parser.add_argument("--title-field", default="title")
    parser.add_argument(
        "--model",
        help="Codex model override; direct/pi models come from memory_write configuration",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        help="Codex reasoning-effort override",
    )
    args = parser.parse_args(argv)
    if args.executor != "codex" and (args.model or args.reasoning_effort):
        parser.error("--model/--reasoning-effort are Codex-only overrides")
    return args


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(row: Mapping[str, Any], field: str, line: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise BenchmarkInputError(f"line {line}: {field!r} must be a non-empty string")
    return value


def _optional_string(row: Mapping[str, Any], field: str, line: int) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BenchmarkInputError(f"line {line}: {field!r} must be a string or null")
    return value


def _safe_source_id(value: str, line: int) -> str:
    if value != value.strip() or value in {".", ".."}:
        raise BenchmarkInputError(
            f"line {line}: source id contains unsafe whitespace/path syntax"
        )
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise BenchmarkInputError(
            f"line {line}: source id must be one path-safe filename component"
        )
    return value


def _case_from_row(
    row: Mapping[str, Any], line: int, args: argparse.Namespace
) -> BenchmarkCase:
    source_id = _safe_source_id(_required_string(row, args.source_id_field, line), line)
    transcript = _required_string(row, args.transcript_field, line)
    date = _required_string(row, args.date_field, line)
    guidance = _optional_string(row, args.guidance_field, line) or ""
    title = _optional_string(row, args.title_field, line)

    raw_duration = row.get(args.duration_seconds_field)
    if raw_duration is None:
        duration_seconds = None
        duration_minutes = None
    elif isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
        raise BenchmarkInputError(
            f"line {line}: {args.duration_seconds_field!r} must be a finite number or null"
        )
    else:
        duration_seconds = float(raw_duration)
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise BenchmarkInputError(
                f"line {line}: {args.duration_seconds_field!r} must be finite and non-negative"
            )
        duration_minutes = duration_seconds / 60.0

    return BenchmarkCase(
        source_id=source_id,
        dataset_line=line,
        transcript=transcript,
        date=date,
        guidance=guidance,
        duration_seconds=duration_seconds,
        duration_minutes=duration_minutes,
        title=title,
    )


def load_cases(
    dataset: Path, args: argparse.Namespace
) -> tuple[list[BenchmarkCase], str]:
    """Load JSONL and select unique prefixes without transforming agent inputs."""
    dataset = dataset.resolve()
    if not dataset.is_file():
        raise BenchmarkInputError(f"dataset does not exist: {dataset}")
    rows: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    with dataset.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkInputError(
                    f"line {line_number}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise BenchmarkInputError(
                    f"line {line_number}: JSON value must be an object"
                )
            case = _case_from_row(raw, line_number, args)
            if case.source_id in seen_ids:
                raise BenchmarkInputError(
                    f"line {line_number}: duplicate source id {case.source_id!r}"
                )
            seen_ids.add(case.source_id)
            rows.append(case)

    if not rows:
        raise BenchmarkInputError("dataset has no cases")
    if args.all:
        selected = rows
    else:
        selected = []
        selected_ids: set[str] = set()
        for prefix in args.source_id_prefixes:
            matches = [case for case in rows if case.source_id.startswith(prefix)]
            if len(matches) != 1:
                raise BenchmarkInputError(
                    f"source-id prefix {prefix!r} matched {len(matches)} cases; expected exactly one"
                )
            if matches[0].source_id in selected_ids:
                raise BenchmarkInputError(
                    f"source-id prefix {prefix!r} selects duplicate case {matches[0].source_id!r}"
                )
            selected.append(matches[0])
            selected_ids.add(matches[0].source_id)
    return selected, _file_sha256(dataset)


def _write_private_text(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    path.chmod(_PRIVATE_FILE_MODE)


def _prepare_output(output: Path) -> tuple[Path, Path]:
    output = output.resolve()
    if output.exists() and not output.is_dir():
        raise BenchmarkInputError(f"output exists and is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise BenchmarkInputError(f"refusing non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
    output.chmod(_PRIVATE_DIRECTORY_MODE)
    _write_private_text(output / ".gitignore", _ARTIFACT_GITIGNORE)
    vault = output / "vault"
    vault.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    return output, vault


def _make_tree_private(root: Path) -> None:
    """Make every regular benchmark artifact owner-only without following links."""
    root.chmod(_PRIVATE_DIRECTORY_MODE)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(_PRIVATE_DIRECTORY_MODE)
        elif path.is_file():
            path.chmod(_PRIVATE_FILE_MODE)


@contextlib.contextmanager
def _private_process_umask():
    """Protect files created by agents and their subprocesses before normalization."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@contextlib.contextmanager
def _isolated_vault_lock(*_args: Any, **_kwargs: Any):
    """No Redis dependency: the benchmark is the only writer of its new vault."""
    yield


def _snapshot(root: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            rel = path.relative_to(root).as_posix()
            snapshot[rel] = {
                "kind": "symlink",
                "target_sha256": _text_sha256(str(path.readlink())),
            }
        elif path.is_file():
            rel = path.relative_to(root).as_posix()
            snapshot[rel] = {
                "kind": "file",
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
    return snapshot


def _diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        "created": sorted(path for path in after if path not in before),
        "modified": sorted(
            path for path in after if path in before and before[path] != after[path]
        ),
        "removed": sorted(path for path in before if path not in after),
    }


def _safe_manifest_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts


def _sanitize_removed(items: Iterable[Any]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            entry: dict[str, Any] = {
                "old_path": str(item.get("old_path") or ""),
                "new_path": str(item.get("new_path") or ""),
            }
            before = item.get("before")
            if isinstance(before, str):
                entry["before_sha256"] = _text_sha256(before)
                entry["before_chars"] = len(before)
            sanitized.append(entry)
        else:
            sanitized.append({"old_path": str(item), "new_path": ""})
    return sanitized


def _serialize_result(
    result: Any, exception: Exception | None = None
) -> dict[str, Any]:
    if exception is not None:
        summary = ""
        return {
            "returned": False,
            "rounds": 0,
            "tool_calls": 0,
            "touched": [],
            "removed": [],
            "errors": [f"{type(exception).__name__}: {exception}"],
            "usage": {},
            "truncated": True,
            "stalled": False,
            "summary_sha256": _text_sha256(summary),
            "summary_chars": 0,
        }
    usage = getattr(result, "usage", {}) or {}
    summary = str(getattr(result, "summary", "") or "")
    return {
        "returned": True,
        "rounds": int(getattr(result, "rounds", 0) or 0),
        "tool_calls": int(getattr(result, "tool_calls", 0) or 0),
        "touched": [str(value) for value in (getattr(result, "touched", []) or [])],
        "removed": _sanitize_removed(getattr(result, "removed", []) or []),
        "errors": [str(value) for value in (getattr(result, "errors", []) or [])],
        "usage": {
            str(key): value
            for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
        "truncated": bool(getattr(result, "truncated", False)),
        "stalled": bool(getattr(result, "stalled", False)),
        # Agent summaries can repeat transcript facts. Keep the benchmark manifest
        # non-plaintext just like its exact transcript/guidance input records; the
        # isolated vault remains the artifact for qualitative inspection.
        "summary_sha256": _text_sha256(summary),
        "summary_chars": len(summary),
    }


def _result_path_issues(
    source_id: str, result: Mapping[str, Any]
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for field in ("touched",):
        for value in result.get(field, []):
            if not _safe_manifest_path(value):
                issues.append(
                    {
                        "code": "unsafe_result_path",
                        "path": str(value),
                        "detail": f"{source_id}:{field}",
                    }
                )
    for item in result.get("removed", []):
        for field in ("old_path", "new_path"):
            value = item.get(field)
            if value and not _safe_manifest_path(value):
                issues.append(
                    {
                        "code": "unsafe_result_path",
                        "path": str(value),
                        "detail": f"{source_id}:removed.{field}",
                    }
                )
    return issues


def _frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None]:
    boundaries = list(_FRONTMATTER_RE.finditer(text))
    if len(boundaries) < 2:
        return None, "missing YAML frontmatter"
    try:
        # Soft dependency: the except below covers hosts without it.
        import yaml

        value = yaml.safe_load(text[boundaries[0].end() : boundaries[1].start()]) or {}
    except Exception as exc:  # noqa: BLE001 - the issue is recorded in the manifest
        return None, f"invalid YAML frontmatter: {exc}"
    if not isinstance(value, dict):
        return None, "frontmatter is not a mapping"
    return value, None


def _substantive(value: str) -> bool:
    for line in value.splitlines():
        payload = re.sub(r"^-\s*(?:\[[ xX]\]\s*)?", "", line.strip()).strip()
        if payload.casefold() not in _PLACEHOLDERS:
            return True
    return False


def _section_values(text: str, regex: re.Pattern[str]) -> dict[str, list[str]]:
    matches = list(regex.finditer(text))
    values: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        values.setdefault(match.group(1).strip().casefold(), []).append(
            text[match.end() : end].strip()
        )
    return values


def _issue(code: str, path: str, detail: str = "") -> dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def _issue_key(issue: Mapping[str, str]) -> tuple[str, str, str]:
    return issue.get("code", ""), issue.get("path", ""), issue.get("detail", "")


def scan_vault(
    vault: Path,
    expected: Mapping[str, BenchmarkCase],
    initial_scaffold: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the vault and report structural invariants without semantic grading."""
    issues: list[dict[str, str]] = []
    snapshot = _snapshot(vault)

    for rel, original in initial_scaffold.items():
        current = snapshot.get(rel)
        if current is None:
            issues.append(_issue("scaffold_removed", rel))
        elif current != original:
            issues.append(_issue("scaffold_modified", rel))

    folded: dict[str, list[str]] = {}
    for rel in snapshot:
        folded.setdefault(rel.casefold(), []).append(rel)
    for siblings in folded.values():
        if len(siblings) > 1:
            issues.append(
                _issue("case_collision", siblings[0], ", ".join(sorted(siblings)))
            )

    expected_notes = {
        f"Conversations/{source_id}.md": case for source_id, case in expected.items()
    }
    actual_conversations: set[str] = set()
    for rel, metadata in snapshot.items():
        path = vault / rel
        if metadata.get("kind") == "symlink":
            issues.append(_issue("symlink", rel))
            continue
        if not rel.endswith(".md"):
            if not (rel.startswith("Templates/Bases/") and rel.endswith(".base")):
                issues.append(_issue("unexpected_file_type", rel))
            continue
        if rel.startswith("Templates/"):
            continue
        if len(PurePosixPath(rel).parts) == 1:
            try:
                hub_text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                issues.append(_issue("invalid_utf8", rel))
                continue
            if (
                "tags:\n  - categories" not in hub_text
                or "![[" not in hub_text
                or ".base]]" not in hub_text
            ):
                issues.append(_issue("invalid_category_hub", rel))
            continue
        if len(PurePosixPath(rel).parts) != 2:
            issues.append(_issue("nested_note", rel))

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(_issue("invalid_utf8", rel))
            continue
        if not text:
            issues.append(_issue("empty_note", rel))
            continue
        if "```" in text or "\\n" in text:
            issues.append(_issue("escaped_or_fenced_note", rel))
        if _UNKNOWN_LINK_RE.search(text):
            issues.append(_issue("unknown_speaker_wikilink", rel))

        h2 = _section_values(text, _H2_RE)
        h3 = _section_values(text, _H3_RE)
        for name, values in h2.items():
            if len(values) > 1:
                issues.append(_issue("duplicate_h2", rel, name))
        for name, values in h3.items():
            if len(values) > 1:
                issues.append(_issue("duplicate_h3", rel, name))

        frontmatter, frontmatter_error = _frontmatter(text)
        if frontmatter_error:
            issues.append(_issue("invalid_frontmatter", rel, frontmatter_error))
            continue
        categories = frontmatter.get("categories")
        if not isinstance(categories, list) or not categories:
            issues.append(_issue("missing_categories", rel))

        parts = PurePosixPath(rel).parts
        folder, filename = parts[0], parts[-1]
        if folder == "People":
            stem = filename[:-3]
            if stem.casefold() == "hermes" or _UNKNOWN_PERSON_RE.match(stem):
                issues.append(_issue("forbidden_person_note", rel))
            for required in ("about", "conversations", "mentions"):
                if len(h2.get(required, [])) != 1:
                    issues.append(_issue("missing_person_section", rel, required))
            if "![[Conversations.base#Person]]" not in text:
                issues.append(_issue("missing_person_embed", rel))
        elif folder == "Topics":
            for required in ("about", "conversations"):
                if len(h2.get(required, [])) != 1:
                    issues.append(_issue("missing_topic_section", rel, required))
            if "![[Conversations.base#Topic]]" not in text:
                issues.append(_issue("missing_topic_embed", rel))
        elif folder == "Conversations":
            actual_conversations.add(rel)
            case = expected_notes.get(rel)
            if case is None:
                issues.append(_issue("unexpected_conversation", rel))
                continue
            if frontmatter.get("conversation_id") != case.source_id:
                issues.append(_issue("conversation_id_mismatch", rel))
            if str(frontmatter.get("date")) != case.date:
                issues.append(_issue("conversation_date_mismatch", rel))
            duration = frontmatter.get("duration_minutes")
            if case.duration_minutes is None:
                if duration is not None:
                    issues.append(_issue("conversation_duration_mismatch", rel))
            else:
                try:
                    # The production canonicalizer serializes trusted duration with
                    # Python's ``:g`` format (six significant digits by default).
                    # Compare against that exact persisted representation, not the
                    # higher-precision seconds/60 input held by the harness.
                    persisted_duration = float(f"{case.duration_minutes:g}")
                    duration_matches = math.isclose(
                        float(duration), persisted_duration, abs_tol=1e-12
                    )
                except (TypeError, ValueError):
                    duration_matches = False
                if not duration_matches:
                    issues.append(_issue("conversation_duration_mismatch", rel))
            if "[[Conversations]]" not in (categories or []):
                issues.append(_issue("conversation_category_mismatch", rel))
            for required in ("summary", "key facts", "action items"):
                values = h3.get(required, [])
                if len(values) != 1:
                    issues.append(_issue("missing_conversation_section", rel, required))
                elif required != "action items" and not _substantive(values[0]):
                    issues.append(_issue("empty_conversation_section", rel, required))
            people = frontmatter.get("people") or []
            if isinstance(people, list) and any(
                "unknown speaker" in str(person).casefold()
                or str(person).casefold() == "[[hermes]]"
                for person in people
            ):
                issues.append(_issue("forbidden_conversation_person", rel))

    for rel in expected_notes:
        if rel not in actual_conversations:
            issues.append(_issue("missing_conversation", rel))

    issues.sort(key=_issue_key)
    return {
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "file_count": len(snapshot),
        "captured_note_count": sum(
            1
            for rel in snapshot
            if rel.endswith(".md") and not rel.startswith("Templates/") and "/" in rel
        ),
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    _write_private_text(temporary, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    path.chmod(_PRIVATE_FILE_MODE)


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


def _safe_runtime_metadata(executor: str, modules: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"operation": "memory_write"}
    if executor == "direct":
        try:
            # Soft dependency: the except below covers hosts without it.
            from backend.model_registry import get_models_registry

            registry = get_models_registry()
            operation = registry.get_llm_operation("memory_write") if registry else None
            if operation is not None:
                model_def = operation.model_def
                metadata.update(
                    {
                        "model_name": str(operation.model_name),
                        "model_provider": str(model_def.model_provider),
                        "registry_model": str(model_def.name),
                        "temperature": operation.temperature,
                        "max_tokens": operation.max_tokens,
                        "reasoning_effort": operation.reasoning_effort,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - provenance must not block the run
            metadata["registry_error"] = f"{type(exc).__name__}: {exc}"
    elif executor == "pi":
        pi_agent = modules["pi_agent"]
        available, detail = pi_agent.pi_executor_available()
        metadata.update(
            {"executor_available": bool(available), "executor_detail": str(detail)}
        )
        try:
            # Soft dependency: the except below covers hosts without it.
            from backend.model_registry import get_models_registry

            registry = get_models_registry()
            if registry is None:
                raise RuntimeError("Chronicle model registry is unavailable")
            settings = pi_agent._pi_settings(registry)
            resolved = pi_agent._resolve_pi_config("memory_write")
            metadata.update(
                {
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
    elif executor == "codex":
        codex_agent = modules["codex_agent"]
        available, detail = codex_agent.codex_executor_available()
        metadata.update(
            {"executor_available": bool(available), "executor_detail": str(detail)}
        )
        try:
            settings = codex_agent._validated_codex_settings()
            model = str(settings.get("model") or "codex-default")
            metadata.update(
                {
                    "model_name": model,
                    "model_provider": "openai_codex_cli",
                    "codex_model": model,
                    "codex_reasoning_effort": settings.get("reasoning_effort") or None,
                    "codex_sandbox_mode": settings["sandbox_mode"],
                    "codex_timeout_seconds": settings["timeout_seconds"],
                    "codex_quota_fallback_disabled": settings.get("max_used_percent")
                    is None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - agent result captures hard failures
            metadata["settings_error"] = f"{type(exc).__name__}: {exc}"
    return metadata


def _load_agent(executor: str, args: argparse.Namespace) -> tuple[type, dict[str, Any]]:
    """Import agents lazily, disable Redis locks, and apply Codex-only overrides."""
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend / "src"))

    # Imported here: backend/src only joins sys.path at runtime (above).
    from backend.services.memory import vault_lock
    from backend.services.memory.agent import vault_tools
    from backend.services.memory.agent.memory_agent import MemoryAgent

    vault_lock.vault_run_lock = _isolated_vault_lock
    vault_lock.vault_note_lock = _isolated_vault_lock
    vault_tools.vault_note_lock = _isolated_vault_lock

    modules: dict[str, Any] = {}
    if executor == "direct":
        agent_type = MemoryAgent
    elif executor == "pi":
        # Imported here: backend/src only joins sys.path at runtime (above).
        from backend.services.memory.agent import pi_agent

        pi_agent.vault_run_lock = _isolated_vault_lock
        modules["pi_agent"] = pi_agent
        agent_type = pi_agent.PiMemoryAgent
    else:
        # Imported here: backend/src only joins sys.path at runtime (above).
        from backend.services.memory.agent import codex_agent

        configured = codex_agent._validated_codex_settings()
        configured.update(
            {
                key: value
                for key, value in {
                    "model": args.model,
                    "reasoning_effort": args.reasoning_effort,
                }.items()
                if value
            }
        )
        # Production may protect the user's interactive Codex quota by delegating a
        # blocked run to the direct agent. That would invalidate an executor A/B run,
        # so the benchmark disables this one internal fallback explicitly.
        configured["max_used_percent"] = None
        configured = codex_agent._validated_codex_settings(configured)
        codex_agent._codex_settings = lambda: configured
        modules["codex_agent"] = codex_agent
        agent_type = codex_agent.CodexMemoryAgent
    return agent_type, _safe_runtime_metadata(executor, modules)


async def _run_case(
    agent_type: type,
    vault: Path,
    case: BenchmarkCase,
    expected: Mapping[str, BenchmarkCase],
    initial_scaffold: Mapping[str, Any],
) -> dict[str, Any]:
    # Imported here: backend/src only joins sys.path at runtime (above).
    from backend.services.memory.conversation_note import (
        ConversationNoteError,
        canonicalize_conversation_note,
        write_source_fallback_conversation_note,
    )

    before = _snapshot(vault)
    prior_expected = {
        source_id: prior_case
        for source_id, prior_case in expected.items()
        if source_id != case.source_id
    }
    before_invariants = scan_vault(vault, prior_expected, initial_scaffold)
    started = time.perf_counter()
    result = None
    exception: Exception | None = None
    try:
        result = await agent_type(vault).run(
            case.transcript,
            case.source_id,
            date=case.date,
            duration_minutes=case.duration_minutes,
            title=case.title,
            guidance=case.guidance,
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - record failure and preserve the source note
        exception = exc
    agent_elapsed = time.perf_counter() - started
    after_agent = _snapshot(vault)
    serialized = _serialize_result(result, exception)

    note_rel = f"Conversations/{case.source_id}.md"
    note = vault / note_rel
    primary_existed = note.is_file() and not note.is_symlink()
    primary_canonical = False
    validation_error: str | None = None
    if primary_existed:
        try:
            canonicalize_conversation_note(
                note,
                conversation_id=case.source_id,
                date=case.date,
                duration_minutes=case.duration_minutes,
                title=case.title,
            )
            primary_canonical = True
        except (ConversationNoteError, OSError) as exc:
            validation_error = str(exc)
            note.unlink(missing_ok=True)
    else:
        validation_error = "exact conversation note missing"
        if note.is_symlink():
            note.unlink(missing_ok=True)

    fallback_written = False
    if not primary_canonical:
        write_source_fallback_conversation_note(
            note,
            transcript=case.transcript,
            conversation_id=case.source_id,
            date=case.date,
            duration_minutes=case.duration_minutes,
            title=case.title,
        )
        fallback_written = True

    final_canonical = False
    final_validation_error: str | None = None
    try:
        canonicalize_conversation_note(
            note,
            conversation_id=case.source_id,
            date=case.date,
            duration_minutes=case.duration_minutes,
            title=case.title,
        )
        final_canonical = True
    except (ConversationNoteError, OSError) as exc:
        final_validation_error = str(exc)

    invariants = scan_vault(vault, expected, initial_scaffold)
    result_path_issues = _result_path_issues(case.source_id, serialized)
    if result_path_issues:
        invariants["issues"] = sorted(
            [*invariants["issues"], *result_path_issues], key=_issue_key
        )
        invariants["issue_count"] = len(invariants["issues"])
        invariants["ok"] = False
    before_issue_keys = {_issue_key(issue) for issue in before_invariants["issues"]}
    after_issue_keys = {_issue_key(issue) for issue in invariants["issues"]}
    invariants["introduced_issues"] = [
        issue
        for issue in invariants["issues"]
        if _issue_key(issue) not in before_issue_keys
    ]
    invariants["introduced_issue_count"] = len(invariants["introduced_issues"])
    invariants["resolved_issues"] = [
        issue
        for issue in before_invariants["issues"]
        if _issue_key(issue) not in after_issue_keys
    ]
    invariants["resolved_issue_count"] = len(invariants["resolved_issues"])
    after_final = _snapshot(vault)
    agent_completed = bool(
        serialized["returned"]
        and not serialized["truncated"]
        and not serialized["stalled"]
    )
    ok = bool(
        agent_completed
        and primary_canonical
        and not invariants["introduced_issue_count"]
    )
    return {
        "source_id": case.source_id,
        "input": case.input_record(),
        "ok": ok,
        "completed": final_canonical,
        "agent_completed": agent_completed,
        "latency_seconds": round(agent_elapsed, 6),
        "total_elapsed_seconds": round(time.perf_counter() - started, 6),
        "result": serialized,
        "conversation_note": {
            "path": note_rel,
            "primary_existed": primary_existed,
            "primary_canonical": primary_canonical,
            "primary_validation_error": validation_error,
            "fallback_written": fallback_written,
            "final_canonical": final_canonical,
            "final_validation_error": final_validation_error,
            "final_sha256": (
                _file_sha256(note) if note.is_file() and not note.is_symlink() else None
            ),
        },
        "filesystem": {
            "agent_diff": _diff(before, after_agent),
            "final_diff": _diff(before, after_final),
        },
        "vault_invariants": invariants,
    }


async def _run(args: argparse.Namespace) -> int:
    args.dataset = args.dataset.resolve()
    cases, dataset_sha256 = load_cases(args.dataset, args)
    output, vault = _prepare_output(args.output)

    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend / "src"))
    # Imported here: backend/src only joins sys.path at runtime (above).
    from backend.services.memory.vault_scaffold import seed_vault_scaffold

    seed_vault_scaffold(vault)
    _make_tree_private(output)
    initial_scaffold = _snapshot(vault)
    agent_type, runtime = _load_agent(args.executor, args)

    manifest_path = output / "manifest.json"
    started_at = datetime.now(timezone.utc)
    manifest: dict[str, Any] = {
        "kind": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "executor": args.executor,
        "runtime": runtime,
        "dataset": {
            "path": str(args.dataset),
            "sha256": dataset_sha256,
            "source_id_field": args.source_id_field,
            "transcript_field": args.transcript_field,
            "date_field": args.date_field,
            "guidance_field": args.guidance_field,
            "duration_seconds_field": args.duration_seconds_field,
            "title_field": args.title_field,
        },
        "selection": {
            "all": bool(args.all),
            "source_id_prefixes": args.source_id_prefixes or [],
            "source_ids": [case.source_id for case in cases],
        },
        "artifact": {
            "root": str(output),
            "vault": "vault",
            "manifest": "manifest.json",
        },
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "git": _git_metadata(backend),
        },
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "runs": [],
        "summary": None,
        "vault": None,
    }
    _atomic_write_json(manifest_path, manifest)

    processed: dict[str, BenchmarkCase] = {}
    for index, case in enumerate(cases, 1):
        print(
            f"[{index}/{len(cases)}] {args.executor}: {case.source_id} ({len(case.transcript)} chars)",
            flush=True,
        )
        processed[case.source_id] = case
        run = await _run_case(agent_type, vault, case, processed, initial_scaffold)
        _make_tree_private(output)
        manifest["runs"].append(run)
        _atomic_write_json(manifest_path, manifest)

    final_invariants = scan_vault(vault, processed, initial_scaffold)
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["vault"] = {
        "file_count": final_invariants["file_count"],
        "captured_note_count": final_invariants["captured_note_count"],
        "invariants": final_invariants,
    }
    manifest["summary"] = {
        "case_count": len(cases),
        "ok_count": sum(bool(run["ok"]) for run in manifest["runs"]),
        "completion_count": sum(bool(run["completed"]) for run in manifest["runs"]),
        "fallback_count": sum(
            bool(run["conversation_note"]["fallback_written"])
            for run in manifest["runs"]
        ),
        "error_case_count": sum(
            bool(run["result"]["errors"]) for run in manifest["runs"]
        ),
        "latency_seconds": round(
            sum(run["latency_seconds"] for run in manifest["runs"]), 6
        ),
        "final_invariant_issue_count": final_invariants["issue_count"],
    }
    _atomic_write_json(manifest_path, manifest)
    _make_tree_private(output)
    print(f"vault: {vault}\nmanifest: {manifest_path}")
    return int(
        manifest["summary"]["ok_count"] != len(cases)
        or manifest["summary"]["completion_count"] != len(cases)
        or not final_invariants["ok"]
    )


def main(argv: list[str] | None = None) -> int:
    try:
        with _private_process_umask():
            args = _parse_args(argv)
            return asyncio.run(_run(args))
    except BenchmarkInputError as exc:
        print(f"benchmark input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
