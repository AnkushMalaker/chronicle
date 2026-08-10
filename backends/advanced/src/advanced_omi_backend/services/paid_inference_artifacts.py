"""Durable, content-addressed records for paid inference subprocesses.

Paid provider output must survive the process that consumed it.  The artifact keeps
the complete request and response, while a small request index makes deterministic
operations (currently timeline analysis) reusable without paying for the same call.
Context-dependent vault mutations are archived for audit but are never replayed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from advanced_omi_backend.config import DATA_DIR

logger = logging.getLogger(__name__)


def _root() -> Path:
    return Path(
        os.getenv("PAID_INFERENCE_ARTIFACT_DIR", DATA_DIR / "paid_inference_artifacts")
    )


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def persist_paid_run(
    *,
    operation: str,
    request: dict[str, Any],
    stdout: str,
    stderr: str,
    result: Any,
    metadata: dict[str, Any] | None = None,
    reusable: bool = False,
) -> tuple[str, str]:
    """Persist one complete provider interaction and return request/artifact hashes."""

    request_hash = canonical_hash(request)
    record = {
        "format": "chronicle-paid-inference-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "request_hash": request_hash,
        "request": request,
        "stdout": stdout,
        "stderr": stderr,
        "result": result,
        "metadata": metadata or {},
        "reusable": reusable,
    }
    artifact_hash = canonical_hash(record)
    base = _root() / operation
    artifact_path = base / "artifacts" / f"{artifact_hash}.json.gz"
    payload = json.dumps(record, ensure_ascii=False, default=str).encode()
    _atomic_write(artifact_path, gzip.compress(payload, compresslevel=6))

    if reusable:
        pointer = json.dumps(
            {"artifact_hash": artifact_hash, "request_hash": request_hash},
            separators=(",", ":"),
        ).encode()
        _atomic_write(base / "requests" / f"{request_hash}.json", pointer)
    return request_hash, artifact_hash


def load_reusable_result(operation: str, request: dict[str, Any]) -> Any | None:
    """Return a prior successful structured result for an identical request."""

    request_hash = canonical_hash(request)
    base = _root() / operation
    pointer_path = base / "requests" / f"{request_hash}.json"
    if not pointer_path.is_file():
        return None
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    artifact_path = base / "artifacts" / f"{pointer['artifact_hash']}.json.gz"
    with gzip.open(artifact_path, "rt", encoding="utf-8") as stream:
        record = json.load(stream)
    if record.get("request_hash") != request_hash or not record.get("reusable"):
        raise ValueError(f"Invalid paid inference cache entry: {artifact_path}")
    return record.get("result")
