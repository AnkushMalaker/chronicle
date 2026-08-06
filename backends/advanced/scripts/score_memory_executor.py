#!/usr/bin/env python3
"""Compare Chronicle memory-executor manifests using structural measurements only.

The scorer never opens or changes a vault.  It reports completion, fallback and error
rates, latency, tool/round/token counts, and the invariant results captured by the
benchmark harness.  It deliberately does not infer semantic correctness without a
separately curated gold dataset.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

MANIFEST_KIND = "chronicle-memory-executor-benchmark"
MANIFEST_SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """A manifest is invalid or cannot be compared fairly."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", type=Path, nargs="+", help="manifest.json paths")
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="stdout format (the scorer never writes files)",
    )
    return parser.parse_args(argv)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{path}: root must be a JSON object")
    if value.get("kind") != MANIFEST_KIND:
        raise ManifestError(f"{path}: unsupported manifest kind {value.get('kind')!r}")
    if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"{path}: unsupported schema version {value.get('schema_version')!r}"
        )
    if not value.get("finished_at") or not isinstance(value.get("runs"), list):
        raise ManifestError(f"{path}: manifest is incomplete")
    if not value["runs"]:
        raise ManifestError(f"{path}: manifest contains no cases")
    value["_path"] = str(path.resolve())
    return value


def _number(value: Any) -> float:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _token_total(usage: Mapping[str, Any]) -> float:
    if _number(usage.get("total_tokens")):
        return _number(usage.get("total_tokens"))
    input_tokens = _number(usage.get("input_tokens")) or _number(
        usage.get("prompt_tokens")
    )
    output_tokens = _number(usage.get("output_tokens")) or _number(
        usage.get("completion_tokens")
    )
    return input_tokens + output_tokens


def summarize_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    runs = manifest["runs"]
    count = len(runs)
    latencies = [_number(run.get("latency_seconds")) for run in runs]
    total_latencies = [_number(run.get("total_elapsed_seconds")) for run in runs]
    rounds = [_number(run.get("result", {}).get("rounds")) for run in runs]
    tools = [_number(run.get("result", {}).get("tool_calls")) for run in runs]

    usage_totals: dict[str, float] = {}
    token_total = 0.0
    error_entries = 0
    error_cases = 0
    introduced_invariant_issues = 0
    introduced_invariant_cases = 0
    for run in runs:
        result = run.get("result", {})
        errors = result.get("errors") or []
        error_entries += len(errors)
        error_cases += bool(errors)
        usage = result.get("usage") or {}
        token_total += _token_total(usage)
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage_totals[str(key)] = usage_totals.get(str(key), 0.0) + float(value)
        run_invariants = run.get("vault_invariants", {})
        issue_count = int(
            run_invariants.get(
                "introduced_issue_count", run_invariants.get("issue_count")
            )
            or 0
        )
        introduced_invariant_issues += issue_count
        introduced_invariant_cases += issue_count > 0

    final_invariants = manifest.get("vault", {}).get("invariants", {})
    ok_count = sum(bool(run.get("ok")) for run in runs)
    completed_count = sum(bool(run.get("completed")) for run in runs)
    agent_completed_count = sum(bool(run.get("agent_completed")) for run in runs)
    primary_canonical_count = sum(
        bool(run.get("conversation_note", {}).get("primary_canonical")) for run in runs
    )
    fallback_count = sum(
        bool(run.get("conversation_note", {}).get("fallback_written")) for run in runs
    )
    truncated_count = sum(bool(run.get("result", {}).get("truncated")) for run in runs)
    stalled_count = sum(bool(run.get("result", {}).get("stalled")) for run in runs)
    return {
        "manifest": manifest.get("_path"),
        "executor": manifest.get("executor"),
        "model": (
            manifest.get("runtime", {}).get("codex_model")
            if manifest.get("executor") == "codex"
            else manifest.get("runtime", {}).get("model_name")
        ),
        "cases": count,
        "ok": ok_count,
        "ok_rate": _rate(ok_count, count),
        "completed": completed_count,
        "completion_rate": _rate(completed_count, count),
        "agent_completed": agent_completed_count,
        "agent_completion_rate": _rate(agent_completed_count, count),
        "primary_canonical": primary_canonical_count,
        "primary_canonical_rate": _rate(primary_canonical_count, count),
        "fallbacks": fallback_count,
        "fallback_rate": _rate(fallback_count, count),
        "error_cases": error_cases,
        "error_case_rate": _rate(error_cases, count),
        "error_entries": error_entries,
        "truncated": truncated_count,
        "stalled": stalled_count,
        "latency_seconds": {
            "total": sum(latencies),
            "mean": statistics.fmean(latencies),
            "median": statistics.median(latencies),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies),
            "total_with_harness": sum(total_latencies),
        },
        "rounds": {"total": sum(rounds), "median": statistics.median(rounds)},
        "tool_calls": {"total": sum(tools), "median": statistics.median(tools)},
        "tokens": {"total": token_total, "by_usage_key": usage_totals},
        "vault_invariants": {
            "final_ok": bool(final_invariants.get("ok")),
            "final_issue_count": int(final_invariants.get("issue_count") or 0),
            "issue_cases": introduced_invariant_cases,
            "case_issue_sum": introduced_invariant_issues,
            "final_issues": final_invariants.get("issues") or [],
        },
    }


def _input_sequence(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    sequence: list[tuple[str, str]] = []
    for index, run in enumerate(manifest["runs"]):
        source_id = run.get("source_id")
        fingerprint = run.get("input", {}).get("fingerprint_sha256")
        if not isinstance(source_id, str) or not isinstance(fingerprint, str):
            raise ManifestError(
                f"{manifest.get('_path')}: case {index + 1} lacks an input fingerprint"
            )
        sequence.append((source_id, fingerprint))
    return sequence


def assert_comparable(manifests: list[Mapping[str, Any]]) -> None:
    baseline = _input_sequence(manifests[0])
    for manifest in manifests[1:]:
        candidate = _input_sequence(manifest)
        if candidate != baseline:
            raise ManifestError(
                f"{manifest.get('_path')}: source order or exact input fingerprints differ from "
                f"{manifests[0].get('_path')}"
            )


def _delta(candidate: float, baseline: float) -> float:
    return candidate - baseline


def _ratio(candidate: float, baseline: float) -> float | None:
    return candidate / baseline if baseline else None


def compare_summaries(summaries: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baseline = summaries[0]
    comparisons: list[dict[str, Any]] = []
    for candidate in summaries[1:]:
        comparisons.append(
            {
                "baseline": baseline["manifest"],
                "candidate": candidate["manifest"],
                "executor": candidate["executor"],
                "delta": {
                    "ok_rate_points": 100
                    * _delta(candidate["ok_rate"], baseline["ok_rate"]),
                    "completion_rate_points": 100
                    * _delta(candidate["completion_rate"], baseline["completion_rate"]),
                    "primary_canonical_rate_points": 100
                    * _delta(
                        candidate["primary_canonical_rate"],
                        baseline["primary_canonical_rate"],
                    ),
                    "fallback_rate_points": 100
                    * _delta(candidate["fallback_rate"], baseline["fallback_rate"]),
                    "error_case_rate_points": 100
                    * _delta(candidate["error_case_rate"], baseline["error_case_rate"]),
                    "final_invariant_issues": candidate["vault_invariants"][
                        "final_issue_count"
                    ]
                    - baseline["vault_invariants"]["final_issue_count"],
                    "median_latency_seconds": _delta(
                        candidate["latency_seconds"]["median"],
                        baseline["latency_seconds"]["median"],
                    ),
                    "p95_latency_seconds": _delta(
                        candidate["latency_seconds"]["p95"],
                        baseline["latency_seconds"]["p95"],
                    ),
                    "tool_calls_total": candidate["tool_calls"]["total"]
                    - baseline["tool_calls"]["total"],
                    "tokens_total": candidate["tokens"]["total"]
                    - baseline["tokens"]["total"],
                },
                "ratio": {
                    "median_latency": _ratio(
                        candidate["latency_seconds"]["median"],
                        baseline["latency_seconds"]["median"],
                    ),
                    "p95_latency": _ratio(
                        candidate["latency_seconds"]["p95"],
                        baseline["latency_seconds"]["p95"],
                    ),
                    "tool_calls_total": _ratio(
                        candidate["tool_calls"]["total"],
                        baseline["tool_calls"]["total"],
                    ),
                    "tokens_total": _ratio(
                        candidate["tokens"]["total"], baseline["tokens"]["total"]
                    ),
                },
            }
        )
    return comparisons


def build_report(manifests: list[Mapping[str, Any]]) -> dict[str, Any]:
    assert_comparable(manifests)
    summaries = [summarize_manifest(manifest) for manifest in manifests]
    return {
        "structural_only": True,
        "semantic_quality_scored": False,
        "input_case_count": len(manifests[0]["runs"]),
        "summaries": summaries,
        "comparisons": compare_summaries(summaries),
    }


def _format_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _print_table(report: Mapping[str, Any]) -> None:
    summaries = report["summaries"]
    labels = [
        f"{summary['executor']}:{Path(summary['manifest']).parent.name}"
        for summary in summaries
    ]
    rows = [
        ("cases", [str(summary["cases"]) for summary in summaries]),
        (
            "ok",
            [
                f"{summary['ok']}/{summary['cases']} ({100 * summary['ok_rate']:.1f}%)"
                for summary in summaries
            ],
        ),
        (
            "primary canonical",
            [
                f"{summary['primary_canonical']}/{summary['cases']} "
                f"({100 * summary['primary_canonical_rate']:.1f}%)"
                for summary in summaries
            ],
        ),
        (
            "fallbacks",
            [
                f"{summary['fallbacks']} ({100 * summary['fallback_rate']:.1f}%)"
                for summary in summaries
            ],
        ),
        (
            "error cases",
            [
                f"{summary['error_cases']} ({100 * summary['error_case_rate']:.1f}%)"
                for summary in summaries
            ],
        ),
        (
            "final invariant issues",
            [
                str(summary["vault_invariants"]["final_issue_count"])
                for summary in summaries
            ],
        ),
        (
            "latency median s",
            [
                _format_number(summary["latency_seconds"]["median"], 3)
                for summary in summaries
            ],
        ),
        (
            "latency p95 s",
            [
                _format_number(summary["latency_seconds"]["p95"], 3)
                for summary in summaries
            ],
        ),
        (
            "tool calls",
            [
                _format_number(summary["tool_calls"]["total"], 0)
                for summary in summaries
            ],
        ),
        (
            "tokens",
            [_format_number(summary["tokens"]["total"], 0) for summary in summaries],
        ),
    ]
    widths = [max(len("metric"), *(len(row[0]) for row in rows))]
    widths.extend(
        max(len(label), *(len(row[1][index]) for row in rows))
        for index, label in enumerate(labels)
    )
    header = ["metric", *labels]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for name, values in rows:
        cells = [name, *values]
        print(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(cells))
        )

    if report["comparisons"]:
        print("\nDeltas versus first manifest (percentage-point deltas for rates):")
        for comparison in report["comparisons"]:
            delta = comparison["delta"]
            ratio = comparison["ratio"]
            print(
                f"- {comparison['executor']}: canonical {delta['primary_canonical_rate_points']:+.1f} pp, "
                f"fallback {delta['fallback_rate_points']:+.1f} pp, errors "
                f"{delta['error_case_rate_points']:+.1f} pp, p95 latency "
                f"{_format_number(ratio['p95_latency'])}x, tokens "
                f"{_format_number(ratio['tokens_total'])}x"
            )
    print("\nStructural measurements only; semantic correctness was not scored.")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifests = [load_manifest(path) for path in args.manifests]
        report = build_report(manifests)
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
