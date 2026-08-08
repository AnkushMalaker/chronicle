"""One structured vision call to the Codex CLI.

Codex is the only path in Chronicle that can look at an image: it takes files on disk
via repeated ``--image`` flags and returns JSON validated against ``--output-schema``.
Both the episode thumbnail picker and the screenshot describer need exactly that, so
the subprocess handling lives here once rather than being copied per caller.

The schema must set ``additionalProperties: false`` and list every property in
``required`` — Codex's structured output rejects anything looser, and it fails with an
opaque error rather than a validation message.
"""

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from advanced_omi_backend.services.memory.agent.codex_agent import (
    codex_executor_available,
)

logger = logging.getLogger(__name__)

REASONING_EFFORTS = {"minimal", "none", "low", "medium", "high"}
DEFAULT_TIMEOUT_SECONDS = 600
# stderr tail kept on failure. Enough for the real error, short enough to log.
_ERROR_TAIL = 2000


class CodexVisionError(RuntimeError):
    """A vision run failed. Distinguishes a run failure from Codex being absent."""


class CodexVisionUnavailable(CodexVisionError):
    """Codex itself is not usable, so no run was attempted.

    Callers treat this as a service-level fault: it says nothing about the image, so
    it must not count against any per-item retry budget.
    """


def codex_vision_settings(settings: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    """Validate a ``{model, reasoning_effort, timeout_seconds}`` mapping.

    ``model`` is required with no default: silently falling back to some other model
    would change what the images are judged by without anything saying so.
    """
    model = str(settings.get("model") or "").strip()
    if not model:
        raise ValueError(f"{label}.model must be explicitly configured")
    reasoning = str(settings.get("reasoning_effort") or "").strip().lower()
    if reasoning and reasoning not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(f"{label}.reasoning_effort must be one of {allowed}")
    timeout = int(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if timeout <= 0:
        raise ValueError(f"{label}.timeout_seconds must be positive")
    return {"model": model, "reasoning_effort": reasoning, "timeout_seconds": timeout}


async def run_codex_vision(
    prompt: str,
    images: Sequence[tuple[str, bytes]],
    schema: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one Codex vision request and return its validated JSON output.

    ``images`` is a sequence of ``(filename, bytes)``. Filenames are meaningful — the
    thumbnail picker names them ``frame-<id>.jpg`` and refers to those ids in its
    prompt — so callers choose them rather than getting positional names.
    """
    if not images:
        raise CodexVisionError("a vision run needs at least one image")
    available, detail = codex_executor_available()
    if not available:
        raise CodexVisionUnavailable(detail)

    with tempfile.TemporaryDirectory(prefix="chronicle-vision-") as temp_dir:
        workspace = Path(temp_dir)
        schema_path = workspace / "output-schema.json"
        output_path = workspace / "output.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            detail,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-m",
            settings["model"],
        ]
        if settings.get("reasoning_effort"):
            command += [
                "-c",
                f'model_reasoning_effort="{settings["reasoning_effort"]}"',
            ]
        for filename, data in images:
            image_path = workspace / filename
            image_path.write_bytes(data)
            command += ["--image", str(image_path)]
        command.append("-")

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=settings["timeout_seconds"],
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CodexVisionError(
                f"vision run exceeded {settings['timeout_seconds']}s"
            ) from exc
        if process.returncode != 0:
            raise CodexVisionError(
                f"vision run failed: {stderr.decode(errors='replace')[-_ERROR_TAIL:]}"
            )
        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexVisionError(
                f"vision run produced no usable JSON: {exc}"
            ) from exc
