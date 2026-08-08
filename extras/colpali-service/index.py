"""Per-user visual index over screenshots, scored with MaxSim.

A ColPali-family model emits one vector per image patch rather than one per image.
Relevance is then MaxSim: for each query token take its best-matching patch, and sum
those maxima. That is what makes it strong on screenshots, where the answer usually
lives in one small region rather than in the picture's overall gist.

Storage is one ``.npy`` per document plus an append-only manifest. Deliberately not a
vector database: a personal corpus of deliberately-saved images is thousands of items,
where brute-force numpy is single-digit milliseconds and an index server is pure
operational cost. One file per document also makes an append a single atomic write,
with no rewrite of a growing blob and no window in which the index is corrupt.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.jsonl"


class VisualIndex:
    """Content of ``/index/{user_id}/``, cached in memory per user."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[np.ndarray, list[dict[str, Any]]]] = {}

    def _user_dir(self, user_id: str) -> Path:
        # Guard against a user id escaping its directory; ids are Mongo ObjectIds.
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("invalid user id")
        return self.root / safe

    def _manifest_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / MANIFEST_NAME

    def _read_manifest(self, user_id: str) -> list[dict[str, Any]]:
        path = self._manifest_path(user_id)
        if not path.exists():
            return []
        # Last entry wins, so a re-embed of the same doc_id supersedes the old one.
        entries: dict[str, dict[str, Any]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from an interrupted append is recoverable:
                    # the vectors are in their own file, so drop the line and move on.
                    logger.warning("Skipping malformed manifest line for %s", user_id)
                    continue
                entries[entry["doc_id"]] = entry
        return list(entries.values())

    def add(
        self,
        user_id: str,
        doc_id: str,
        vectors: np.ndarray,
        metadata: dict[str, Any],
        model: str,
    ) -> int:
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        target = user_dir / f"{doc_id}.npy"
        temporary = target.with_suffix(".npy.part")
        # Write through a handle: np.save appends ".npy" to any path that lacks it,
        # which would silently produce "<doc>.npy.part.npy" and break the rename.
        with temporary.open("wb") as handle:
            np.save(handle, vectors.astype(np.float16))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        entry = {
            "doc_id": doc_id,
            "patches": int(vectors.shape[0]),
            "dim": int(vectors.shape[1]),
            "model": model,
            "metadata": metadata,
        }
        with self._manifest_path(user_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with self._lock:
            self._cache.pop(user_id, None)
        return entry["patches"]

    def remove(self, user_id: str, doc_id: str) -> bool:
        target = self._user_dir(user_id) / f"{doc_id}.npy"
        existed = target.exists()
        target.unlink(missing_ok=True)
        remaining = [e for e in self._read_manifest(user_id) if e["doc_id"] != doc_id]
        manifest = self._manifest_path(user_id)
        if manifest.exists():
            temporary = manifest.with_suffix(".jsonl.part")
            temporary.write_text(
                "".join(json.dumps(e) + "\n" for e in remaining), encoding="utf-8"
            )
            os.replace(temporary, manifest)
        with self._lock:
            self._cache.pop(user_id, None)
        return existed

    def documents(self, user_id: str, model: Optional[str] = None) -> list[str]:
        """Doc ids currently indexed, optionally only those built by ``model``.

        Filtering by model is what lets the backend notice a model change and
        re-embed, instead of silently mixing incomparable vector spaces.
        """
        return [
            entry["doc_id"]
            for entry in self._read_manifest(user_id)
            if model is None or entry.get("model") == model
        ]

    def _load(self, user_id: str) -> tuple[np.ndarray, list[dict[str, Any]]]:
        with self._lock:
            cached = self._cache.get(user_id)
            if cached is not None:
                return cached
        entries = self._read_manifest(user_id)
        blocks: list[np.ndarray] = []
        kept: list[dict[str, Any]] = []
        user_dir = self._user_dir(user_id)
        for entry in entries:
            path = user_dir / f"{entry['doc_id']}.npy"
            if not path.exists():
                continue
            block = np.load(path).astype(np.float32)
            entry = {**entry, "offset": sum(b.shape[0] for b in blocks)}
            blocks.append(block)
            kept.append(entry)
        stacked = (
            np.concatenate(blocks, axis=0)
            if blocks
            else np.zeros((0, 0), dtype=np.float32)
        )
        with self._lock:
            self._cache[user_id] = (stacked, kept)
        return stacked, kept

    def search(
        self, user_id: str, query: np.ndarray, limit: int, model: str
    ) -> list[dict[str, Any]]:
        vectors, entries = self._load(user_id)
        if not entries or vectors.size == 0:
            return []
        query = query.astype(np.float32)
        similarity = query @ vectors.T
        hits = []
        for entry in entries:
            if entry.get("model") != model:
                continue
            start = entry["offset"]
            end = start + entry["patches"]
            # MaxSim: each query token scores against its single best patch.
            score = float(similarity[:, start:end].max(axis=1).sum())
            hits.append(
                {
                    "doc_id": entry["doc_id"],
                    "score": score,
                    "metadata": entry.get("metadata") or {},
                }
            )
        hits.sort(key=lambda hit: hit["score"], reverse=True)
        return hits[:limit]

    def stats(self) -> dict[str, int]:
        users = [p for p in self.root.glob("*") if p.is_dir()]
        return {
            "users": len(users),
            "documents": sum(len(list(p.glob("*.npy"))) for p in users),
        }

    def user_ids(self) -> Iterator[str]:
        for path in self.root.glob("*"):
            if path.is_dir():
                yield path.name
