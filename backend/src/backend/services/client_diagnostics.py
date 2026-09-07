"""Durable, user-scoped diagnostic logs pushed by Chronicle clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import DATA_DIR

MAX_CLIENT_DIAGNOSTIC_BYTES = 2_100_000
CLIENT_DIAGNOSTICS_DIR = DATA_DIR / "client_diagnostics"
_UPLOAD_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


def _bounded_metadata(value: str | None, *, limit: int = 200) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.strip().splitlines())
    return cleaned[:limit] or None


def _write_upload(
    *,
    root: Path,
    user_id: str,
    upload_id: str,
    content: bytes,
    metadata: dict[str, Any],
) -> None:
    user_dir = root / Path(user_id).name
    user_dir.mkdir(parents=True, exist_ok=True)

    log_path = user_dir / f"{upload_id}.log"
    metadata_path = user_dir / f"{upload_id}.json"
    token = secrets.token_hex(6)
    log_tmp = user_dir / f".{upload_id}.{token}.log.tmp"
    metadata_tmp = user_dir / f".{upload_id}.{token}.json.tmp"
    try:
        log_tmp.write_bytes(content)
        metadata_tmp.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(log_tmp, log_path)
        os.replace(metadata_tmp, metadata_path)
    finally:
        log_tmp.unlink(missing_ok=True)
        metadata_tmp.unlink(missing_ok=True)


async def store_client_diagnostic(
    *,
    user_id: str,
    content: bytes,
    platform: str | None = None,
    app_version: str | None = None,
    build_version: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Persist one bounded client log and return its receipt metadata."""

    if not content:
        raise ValueError("diagnostic log is empty")
    if len(content) > MAX_CLIENT_DIAGNOSTIC_BYTES:
        raise ValueError("diagnostic log exceeds the size limit")
    content.decode("utf-8")

    now = datetime.now(timezone.utc)
    upload_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(6)}"
    metadata: dict[str, Any] = {
        "upload_id": upload_id,
        "user_id": user_id,
        "received_at": now.isoformat(),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "platform": _bounded_metadata(platform),
        "app_version": _bounded_metadata(app_version),
        "build_version": _bounded_metadata(build_version),
        "device_id": _bounded_metadata(device_id),
    }
    await asyncio.to_thread(
        _write_upload,
        root=CLIENT_DIAGNOSTICS_DIR,
        user_id=user_id,
        upload_id=upload_id,
        content=content,
        metadata=metadata,
    )
    return metadata


def _owned_paths(user_id: str, upload_id: str) -> tuple[Path, Path]:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise FileNotFoundError(upload_id)
    user_dir = CLIENT_DIAGNOSTICS_DIR / Path(user_id).name
    return user_dir / f"{upload_id}.log", user_dir / f"{upload_id}.json"


async def read_client_diagnostic(user_id: str, upload_id: str) -> str:
    log_path, _ = _owned_paths(user_id, upload_id)
    return await asyncio.to_thread(log_path.read_text, encoding="utf-8")


async def list_client_diagnostics(
    user_id: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    user_dir = CLIENT_DIAGNOSTICS_DIR / Path(user_id).name

    def _read() -> list[dict[str, Any]]:
        if not user_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(user_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return rows

    return await asyncio.to_thread(_read)
