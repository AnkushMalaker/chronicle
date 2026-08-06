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
    ) -> TimelineAgentResult:
        usage = await asyncio.to_thread(self._check_quota)
        available, detail = codex_executor_available()
        if not available:
            raise RuntimeError(detail)
        sandbox_mode = str(self.settings.get("sandbox_mode") or "workspace-write")
        schema_path = workspace / "output-schema.json"
        output_path = workspace / "timeline-result.json"
        schema_path.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
        prompt = build_prompt(output_path.name)
        if existing_episodes:
            prompt += (
                "\nThe previous active generation is supplied only as revision context:\n"
                + json.dumps(existing_episodes, default=str)[:30000]
            )
        command = [
            detail,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            sandbox_mode,
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        model = self.settings.get("model")
        reasoning_effort = self.settings.get("reasoning_effort")
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
            _, stderr = await asyncio.wait_for(
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
        if not output_path.is_file():
            raise RuntimeError("Codex timeline analysis produced no structured result")
        return TimelineAgentResult.model_validate_json(
            output_path.read_text(encoding="utf-8")
        )
