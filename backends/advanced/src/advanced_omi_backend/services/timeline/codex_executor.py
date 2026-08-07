"""Codex CLI implementation of the semantic timeline executor contract."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from advanced_omi_backend.services.memory.agent import codex_quota
from advanced_omi_backend.services.memory.agent.codex_agent import (
    codex_executor_available,
)

from .contracts import TimelineAgentResult, TimelineEvidenceManifest
from .prompt import OUTPUT_SCHEMA, build_prompt


class TimelineQuotaDeferred(RuntimeError):
    def __init__(self, message: str, usage: dict[str, Any] | None = None):
        super().__init__(message)
        self.usage = usage or {}


_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _parse_usage(stdout: bytes) -> dict[str, int]:
    """Total token usage across the run's turns from Codex's JSONL event stream.

    Codex emits one ``turn.completed`` event per turn, each carrying that turn's usage,
    so an agentic multi-turn run has several and they must be summed. Malformed or
    absent events are not an error — usage is accounting, not a result.
    """

    totals: dict[str, int] = {}
    turns = 0
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{") or "turn.completed" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage") or {}
        turns += 1
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int):
                totals[field] = totals.get(field, 0) + value
    if turns:
        totals["turns"] = turns
    return totals


class CodexTimelineExecutor:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def _check_quota(self) -> dict[str, Any]:
        payload = codex_quota.read_rate_limits()
        used = codex_quota.bucket_used_percent(
            payload, str(self.settings.get("limit_id") or "")
        )
        threshold = int(self.settings.get("max_used_percent", 80))
        if used is not None and used >= threshold:
            raise TimelineQuotaDeferred(
                f"Codex quota is {used}% used (timeline limit {threshold}%)",
                {"used_percent": used},
            )
        return {"used_percent": used} if used is not None else {}

    async def analyze(
        self,
        workspace: Path,
        manifest: TimelineEvidenceManifest,
        existing_episodes: list[dict[str, Any]],
        pinned_episodes: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
    ) -> TimelineAgentResult:
        usage = await asyncio.to_thread(self._check_quota)
        available, detail = codex_executor_available()
        if not available:
            raise RuntimeError(detail)
        sandbox_mode = str(self.settings.get("sandbox_mode") or "workspace-write")
        schema_path = workspace / "output-schema.json"
        output_path = workspace / "timeline-result.json"
        # MUST differ from output_path. The prompt tells the agent to write its answer to
        # timeline-result.json; pointing --output-last-message at that same file made
        # Codex overwrite the finished result with whatever its last chat message was.
        # Under --output-schema every chat message is schema-shaped, so the agent's
        # progress narration ("kind": "task", "Inspect Chronicle day inputs") replaced the
        # real episodes — silently, and only when the run happened to end on narration.
        last_message_path = workspace / "last-agent-message.json"
        schema_path.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
        prompt = build_prompt(output_path.name)
        if existing_episodes:
            prompt += (
                "\nThe previous active generation is supplied only as revision context:\n"
                + json.dumps(existing_episodes, default=str)[:30000]
            )
        if pinned_episodes:
            prompt += (
                "\nThese confirmed episodes are settled. Their intervals are already "
                "accounted for — do not re-segment them, emit episodes overlapping "
                "them, or mark their time unassigned:\n"
                + json.dumps(pinned_episodes, default=str)[:30000]
            )
        command = [
            detail,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            # JSONL events on stdout are the only place Codex reports token usage; the
            # structured result still comes from --output-last-message, so this only
            # adds accounting. Without it a timeline run costs an unknown amount.
            "--json",
            "--sandbox",
            sandbox_mode,
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(last_message_path),
        ]
        model = self.settings.get("model")
        reasoning_effort = reasoning_effort or self.settings.get("reasoning_effort")
        if model:
            command.extend(["-m", str(model)])
        if reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        command.append("-")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "RUST_LOG": os.environ.get("RUST_LOG", "error")},
        )
        timeout = int(self.settings.get("timeout_seconds", 1800))
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode()), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"Codex timeline analysis timed out after {timeout}s")
        diagnostic = stderr.decode(errors="replace")[-4000:]
        if process.returncode != 0:
            lowered = diagnostic.lower()
            if (
                "rate limit" in lowered
                or "usage limit" in lowered
                or "quota" in lowered
            ):
                raise TimelineQuotaDeferred(
                    diagnostic or "Codex quota exhausted", usage
                )
            raise RuntimeError(
                f"Codex timeline analysis exited {process.returncode}: {diagnostic}"
            )
        # The file the agent was told to write is authoritative. The last chat message is
        # only a fallback for a run that answered inline without writing it.
        source = output_path if output_path.is_file() else last_message_path
        if not source.is_file():
            raise RuntimeError("Codex timeline analysis produced no structured result")
        result = TimelineAgentResult.model_validate_json(
            source.read_text(encoding="utf-8")
        )
        result.usage = {**usage, **_parse_usage(stdout)}
        return result
