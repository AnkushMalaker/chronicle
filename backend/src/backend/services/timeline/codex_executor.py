"""Codex CLI implementation of the semantic timeline executor contract."""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from backend.services.codex_langfuse import upload_codex_trace
from backend.services.inference_artifacts import (
    invalidate_reusable_result,
    load_reusable_result,
    load_reusable_run,
    persist_inference_run,
    promote_inference_run,
)
from backend.services.memory.agent import codex_quota
from backend.services.memory.agent.codex_agent import codex_executor_available

from .contracts import (
    EvidenceBundle,
    InterpretationResult,
    SeparationResult,
    StageInferenceProvenance,
)
from .prompt import (
    INTERPRETATION_OUTPUT_SCHEMA,
    INTERPRETATION_PROMPT_VERSION,
    SEPARATION_OUTPUT_SCHEMA,
    SEPARATION_PROMPT_VERSION,
    build_interpretation_prompt,
    build_separation_prompt,
)

logger = logging.getLogger(__name__)


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

_GENERATED_WORKSPACE_FILES = {
    "interpretation-last-message.json",
    "interpretation-output-schema.json",
    "separation-last-message.json",
    "separation-output-schema.json",
}


def _workspace_fingerprint(workspace: Path) -> list[dict[str, Any]]:
    """Hash every agent-readable input so cache reuse cannot cross evidence changes."""

    files: list[dict[str, Any]] = []
    for path in sorted(
        candidate for candidate in workspace.rglob("*") if candidate.is_file()
    ):
        relative = path.relative_to(workspace).as_posix()
        if relative in _GENERATED_WORKSPACE_FILES:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return files


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

    async def _run_range_stage(
        self,
        workspace: Path,
        *,
        stage: str,
        prompt: str,
        schema: dict[str, Any],
        result_type: type[SeparationResult] | type[InterpretationResult],
        request: dict[str, Any],
        reasoning_effort: str | None,
        validate_result: (
            Callable[[SeparationResult | InterpretationResult], None] | None
        ) = None,
    ) -> SeparationResult | InterpretationResult:
        """Execute one strict range stage; orchestration stays in the shared executor."""

        operation = f"codex_timeline_{stage}"
        schema_path = workspace / f"{stage}-output-schema.json"
        output_path = workspace / f"{stage}-last-message.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        sandbox_mode = str(self.settings.get("sandbox_mode") or "workspace-write")
        model = self.settings.get("model")
        reasoning_effort = reasoning_effort or self.settings.get("reasoning_effort")
        service_tier = str(self.settings.get("service_tier") or "").strip().lower()
        if service_tier not in {"", "priority"}:
            raise ValueError("timeline.codex.service_tier must be empty or priority")
        request = {
            **request,
            "prompt": prompt,
            "output_schema": schema,
            "model": model or "codex-default",
            "reasoning_effort": reasoning_effort or "",
            "service_tier": service_tier,
            "sandbox_mode": sandbox_mode,
            "required_runtime_capabilities": ["code_mode_host"],
            "workspace_files": await asyncio.to_thread(
                _workspace_fingerprint, workspace
            ),
        }
        try:
            cached_run = await asyncio.to_thread(load_reusable_run, operation, request)
        except Exception:
            logger.exception("Codex timeline %s cache lookup failed", stage)
            await asyncio.to_thread(invalidate_reusable_result, operation, request)
            cached_run = None
        if cached_run is not None:
            cached = cached_run.result
            try:
                cached_result = result_type.model_validate(cached)
                if validate_result is not None:
                    validate_result(cached_result)
            except Exception as exc:
                logger.warning(
                    "Rejecting invalid cached Codex timeline %s result: %s",
                    stage,
                    exc,
                )
                await asyncio.to_thread(invalidate_reusable_result, operation, request)
                await asyncio.to_thread(
                    persist_inference_run,
                    operation=operation,
                    request=request,
                    stdout="",
                    stderr="",
                    result=cached,
                    metadata={
                        "stage": stage,
                        "cache_rejected": True,
                        "validation_error": f"{type(exc).__name__}: {exc}",
                    },
                    reusable=False,
                )
            else:
                logger.info("Reusing cached Codex timeline %s result", stage)
                cached_result.inference_provenance = StageInferenceProvenance(
                    operation=operation,
                    request_hash=cached_run.request_hash,
                    artifact_hash=cached_run.artifact_hash,
                    cache_hit=True,
                )
                return cached_result

        usage = await asyncio.to_thread(self._check_quota)
        available, detail = codex_executor_available()
        if not available:
            raise RuntimeError(detail)
        command = [
            detail,
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--sandbox",
            sandbox_mode,
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if model:
            command.extend(["-m", str(model)])
        if reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if service_tier:
            command.extend(["-c", f'service_tier="{service_tier}"'])
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
            await asyncio.to_thread(
                persist_inference_run,
                operation=operation,
                request=request,
                result=None,
                metadata={"error": f"timeout after {timeout}s", "stage": stage},
                reusable=False,
            )
            raise RuntimeError(
                f"Codex timeline {stage} timed out after {timeout}s"
            ) from None
        await asyncio.to_thread(
            upload_codex_trace,
            stdout.decode(errors="replace"),
            operation=f"timeline_{stage}",
        )
        diagnostic = stderr.decode(errors="replace")[-4000:]
        if process.returncode != 0:
            await asyncio.to_thread(
                persist_inference_run,
                operation=operation,
                request=request,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                result=None,
                metadata={"returncode": process.returncode, "stage": stage},
                reusable=False,
            )
            if any(
                marker in diagnostic.lower()
                for marker in ("rate limit", "usage limit", "quota")
            ):
                raise TimelineQuotaDeferred(
                    diagnostic or "Codex quota exhausted", usage
                )
            raise RuntimeError(
                f"Codex timeline {stage} exited {process.returncode}: {diagnostic}"
            )
        if not output_path.is_file():
            raise RuntimeError(f"Codex timeline {stage} produced no structured result")
        raw_result = output_path.read_text(encoding="utf-8")
        try:
            result = result_type.model_validate_json(raw_result)
        except Exception as exc:
            await asyncio.to_thread(
                persist_inference_run,
                operation=operation,
                request=request,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                result={"raw_structured_output": raw_result},
                metadata={"error": f"{type(exc).__name__}: {exc}", "stage": stage},
                reusable=False,
            )
            raise
        metadata = {
            "returncode": process.returncode,
            "stage": stage,
            "usage": {**usage, **_parse_usage(stdout)},
            **(
                {"validation_status": "accepted"} if validate_result is not None else {}
            ),
        }
        if validate_result is not None:
            try:
                validate_result(result)
            except Exception as exc:
                await asyncio.to_thread(
                    persist_inference_run,
                    operation=operation,
                    request=request,
                    stdout=stdout.decode(errors="replace"),
                    stderr=stderr.decode(errors="replace"),
                    result=result.model_dump(mode="json"),
                    metadata={
                        **metadata,
                        "validation_error": f"{type(exc).__name__}: {exc}",
                    },
                    reusable=False,
                )
                raise
        try:
            request_hash, artifact_hash = await asyncio.to_thread(
                persist_inference_run,
                operation=operation,
                request=request,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                result=result.model_dump(mode="json"),
                metadata=metadata,
                reusable=False,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to persist successful Codex timeline {stage} artifact"
            ) from exc
        if validate_result is not None:
            try:
                await asyncio.to_thread(
                    promote_inference_run,
                    operation,
                    request_hash,
                    artifact_hash,
                )
            except Exception:
                logger.exception("Failed to promote Codex timeline %s artifact", stage)
        result.inference_provenance = StageInferenceProvenance(
            operation=operation,
            request_hash=request_hash,
            artifact_hash=artifact_hash,
            cache_hit=False,
        )
        return result

    async def separate(
        self,
        workspace: Path,
        bundle: EvidenceBundle,
        *,
        reasoning_effort: str | None = None,
        validation_feedback: str | None = None,
        validate_result: Callable[[SeparationResult], None] | None = None,
    ) -> SeparationResult:
        prompt = build_separation_prompt(
            evidence_guide=(
                "Read README.md, anchors.json, and windows/index.json, then process "
                "every numbered window JSON in order."
            )
        )
        prompt += (
            "\n\nHuman rejected activities (unchanged evidence must not recreate them):\n"
            + json.dumps(bundle.activity_rejections, default=str)
        )
        prompt += (
            "\n\nExisting exact episode revisions:\n"
            + json.dumps(bundle.existing_episodes, default=str)[:30000]
        )
        prompt += (
            "\n\nField-confirmed episode revisions:\n"
            + json.dumps(bundle.pinned_episodes, default=str)[:30000]
        )
        if validation_feedback:
            prompt += (
                "\n\nDeterministic validation feedback:\n" + validation_feedback[:4000]
            )
        return await self._run_range_stage(
            workspace,
            stage="separation",
            prompt=prompt,
            schema=SEPARATION_OUTPUT_SCHEMA,
            result_type=SeparationResult,
            request={
                "executor": "codex",
                "stage": "separation",
                "prompt_version": SEPARATION_PROMPT_VERSION,
                "manifest": bundle.manifest.model_dump(mode="json"),
                "evidence_revision": bundle.evidence_revision,
                "activity_rejections": bundle.activity_rejections,
                "existing_episodes": bundle.existing_episodes,
                "pinned_episodes": bundle.pinned_episodes,
                "validation_feedback": validation_feedback or "",
            },
            reasoning_effort=reasoning_effort,
            validate_result=validate_result,
        )

    async def interpret(
        self,
        workspace: Path,
        bundle: EvidenceBundle,
        separation: SeparationResult,
        *,
        reasoning_effort: str | None = None,
        validate_result: Callable[[InterpretationResult], None] | None = None,
    ) -> InterpretationResult:
        prompt = build_interpretation_prompt()
        prompt += "\n\nValidated hypotheses:\n" + separation.model_dump_json()
        return await self._run_range_stage(
            workspace,
            stage="interpretation",
            prompt=prompt,
            schema=INTERPRETATION_OUTPUT_SCHEMA,
            result_type=InterpretationResult,
            request={
                "executor": "codex",
                "stage": "interpretation",
                "prompt_version": INTERPRETATION_PROMPT_VERSION,
                "manifest": bundle.manifest.model_dump(mode="json"),
                "evidence_revision": bundle.evidence_revision,
                "separation": separation.model_dump(mode="json"),
            },
            reasoning_effort=reasoning_effort,
            validate_result=validate_result,
        )
