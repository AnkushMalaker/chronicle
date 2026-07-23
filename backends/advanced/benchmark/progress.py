"""Append-only JSONL progress tracker for resume-safe benchmark runs.

One file per run at ``runs/<run_id>/progress.jsonl``. Each line is a JSON
record describing one LongMemEval instance. Writes are atomic-per-line and
fsynced so a power cut between instances loses no completed work.

Statuses (forward-only within a run):
    pending → ingesting → ingested → answered → judged → done
                                                       ↘ error

On resume the runner reads the file, treats only ``done`` as final, and
restarts everything else from the cleanup-then-ingest phase. Per-user
state is cheap to recreate (``cleanup_user`` wipes all four layers),
so re-doing partial work is the safer choice over trying to recover
mid-pipeline state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

logger = logging.getLogger(__name__)

Status = Literal[
    "pending", "ingesting", "ingested", "answered", "judged", "done", "error"
]


@dataclass
class ProgressEntry:
    question_id: str
    status: Status
    user_id: str
    started_at: str = field(default_factory=lambda: _now_iso())
    completed_at: Optional[str] = None
    answer: Optional[str] = None
    score: Optional[bool] = None
    question_type: Optional[str] = None
    judge_model: Optional[str] = None
    extraction_model: Optional[str] = None
    error: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Stable, sortable id with a uuid suffix so two runs in the same minute don't collide."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


class ProgressFile:
    """Append-only JSONL writer + index over a single ``progress.jsonl``.

    Multiple records per question_id are allowed; the *latest* line wins
    when materializing the index. This keeps writes O(1) and survives
    crashes mid-line because a torn final line is just discarded on load.
    """

    def __init__(self, run_dir: Path):
        self._run_dir = run_dir
        self._path = run_dir / "progress.jsonl"
        self._lock = threading.Lock()
        self._index: dict[str, ProgressEntry] = {}
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        loaded = 0
        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Skipping torn JSONL line in %s", self._path)
                    continue
                qid = record.get("question_id")
                if not qid:
                    continue
                self._index[qid] = ProgressEntry(**record)
                loaded += 1
        logger.info("Loaded %d progress records (unique=%d) from %s",
                    loaded, len(self._index), self._path)

    def get(self, question_id: str) -> Optional[ProgressEntry]:
        return self._index.get(question_id)

    def is_done(self, question_id: str) -> bool:
        entry = self._index.get(question_id)
        return entry is not None and entry.status == "done"

    def all_entries(self) -> Iterator[ProgressEntry]:
        return iter(list(self._index.values()))

    def append(self, entry: ProgressEntry) -> None:
        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._index[entry.question_id] = entry

    def update(self, question_id: str, **fields) -> ProgressEntry:
        """Read-modify-append: derive an updated entry from the latest one and append it."""
        prior = self._index.get(question_id)
        if prior is None:
            raise KeyError(f"No prior entry for {question_id!r}")
        merged = ProgressEntry(**{**asdict(prior), **fields})
        if merged.status in ("done", "error") and merged.completed_at is None:
            merged.completed_at = _now_iso()
        self.append(merged)
        return merged


class JudgeCache:
    """Disk cache for judge calls — write-then-rename for crash safety.

    Key fields together: question, answer, ground_truth, model, question_type,
    abstention. If the cache file is corrupted (e.g., killed mid-flush before
    the rename), it just starts empty.
    """

    def __init__(self, run_dir: Path):
        self._path = run_dir / "judge_cache.json"
        self._cache: dict[str, dict] = {}
        if self._path.exists():
            try:
                self._cache = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("judge_cache.json unreadable, starting empty: %s", exc)

    @staticmethod
    def make_key(
        *,
        question: str,
        answer: str,
        ground_truth: str,
        model: str,
        question_type: str,
        abstention: bool,
    ) -> str:
        import hashlib

        h = hashlib.sha256()
        for part in (
            question_type,
            "abs" if abstention else "std",
            model,
            question,
            ground_truth,
            answer,
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x1f")
        return h.hexdigest()

    def get(self, key: str) -> Optional[dict]:
        return self._cache.get(key)

    def put(self, key: str, value: dict) -> None:
        self._cache[key] = value
        # Atomic write: tmp file + rename
        fd, tmp = tempfile.mkstemp(prefix="judge_cache.", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def open_run(run_id: Optional[str], runs_root: Path = Path("runs")) -> tuple[str, Path, ProgressFile, JudgeCache]:
    """Open or create a run directory; returns ``(run_id, run_dir, progress, judge_cache)``."""
    rid = run_id or new_run_id()
    run_dir = runs_root / rid
    progress = ProgressFile(run_dir)
    cache = JudgeCache(run_dir)
    return rid, run_dir, progress, cache
