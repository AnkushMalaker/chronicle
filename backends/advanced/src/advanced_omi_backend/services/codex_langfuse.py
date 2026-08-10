"""Bridge non-interactive Codex runs to the official Langfuse Codex plugin.

Codex currently does not dispatch the plugin's Stop hook for ``codex exec``.  Chronicle
therefore supplies the same hook payload after the subprocess exits.  The plugin still
owns transcript parsing, span construction, usage accounting, and deduplication.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if "thread.started" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
    return None


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _plugin_entrypoint(codex_home: Path) -> Path | None:
    candidates = sorted(
        codex_home.glob(
            "plugins/cache/codex-observability-plugin/tracing/*/dist/index.mjs"
        ),
        reverse=True,
    )
    return candidates[0] if candidates else None


def upload_codex_trace(stdout: str, *, operation: str) -> bool:
    """Upload one saved rollout through Langfuse's official Codex plugin.

    Tracing is auxiliary and deliberately fails open: a trace outage must not cause a
    second paid model call.  ``False`` means tracing was unavailable or failed.
    """

    if not (
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"))
    ):
        return False
    thread_id = _thread_id(stdout)
    if not thread_id:
        logger.warning("Codex trace upload skipped: stdout had no thread id")
        return False
    codex_home = _codex_home()
    rollouts = sorted(
        (codex_home / "sessions").glob(f"**/rollout-*{thread_id}.jsonl"),
        reverse=True,
    )
    plugin = _plugin_entrypoint(codex_home)
    if not rollouts or plugin is None:
        logger.warning(
            "Codex trace upload skipped: rollout=%s official_plugin=%s",
            bool(rollouts),
            bool(plugin),
        )
        return False
    payload = {
        "session_id": thread_id,
        "turn_id": None,
        "transcript_path": str(rollouts[0]),
        "hook_event_name": "Stop",
    }
    existing_tags = os.environ.get("LANGFUSE_CODEX_TAGS", "")
    try:
        parsed_tags = json.loads(existing_tags) if existing_tags else []
    except ValueError:
        parsed_tags = existing_tags.split(",")
    tags = [str(tag).strip() for tag in parsed_tags if str(tag).strip()]
    tags.extend(["chronicle", operation])
    existing_metadata = os.environ.get("LANGFUSE_CODEX_METADATA", "")
    try:
        metadata = json.loads(existing_metadata) if existing_metadata else {}
    except ValueError:
        metadata = {}
    metadata.update({"chronicle.operation": operation, "chronicle.executor": "codex"})
    env = {
        **os.environ,
        "TRACE_TO_LANGFUSE": "true",
        "LANGFUSE_BASE_URL": os.environ.get("LANGFUSE_BASE_URL")
        or os.environ["LANGFUSE_HOST"],
        "LANGFUSE_CODEX_TAGS": json.dumps(sorted(set(tags))),
        "LANGFUSE_CODEX_METADATA": json.dumps(metadata),
    }
    try:
        completed = subprocess.run(
            ["node", str(plugin)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("Official Langfuse Codex trace upload failed")
        return False
    if completed.returncode != 0:
        logger.warning(
            "Official Langfuse Codex trace upload exited %d: %s",
            completed.returncode,
            completed.stderr[-2000:],
        )
        return False
    logger.info("Uploaded native Codex trace to Langfuse: thread=%s", thread_id)
    return True
