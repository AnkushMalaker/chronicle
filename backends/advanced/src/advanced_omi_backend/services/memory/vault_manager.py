"""Conversation document vault manager.

Provides read/write access to per-user conversation document markdown files.
Layout: ``{base_dir}/{user_id}/{conversation_id}.md``

Same pattern as ``services/knowledge_graph/kb.py``: mtime-cache,
path-traversal guard, pure file I/O.
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("memory_service.vault")

_DEFAULT_BASE_DIR = Path(os.getenv("DATA_DIR", "/app/data")) / "conversation_docs"


class ConvDocVaultManager:
    """Manages per-user conversation document .md files on disk.

    File layout:
        {base_dir}/{user_id}/{conversation_id}.md

    Thread-safe for reads. Concurrent writes from different processes
    result in last-write-wins.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self._base_dir = base_dir or _DEFAULT_BASE_DIR
        # mtime cache: "{user_id}/{conv_id}" -> (mtime, content)
        self._cache: Dict[str, Tuple[float, str]] = {}

    def _safe_path(self, user_id: str, conv_id: str) -> Path:
        """Get the .md path with path-traversal guard."""
        safe_uid = Path(user_id).name
        safe_cid = Path(conv_id).name
        return self._base_dir / safe_uid / f"{safe_cid}.md"

    def write_doc(self, user_id: str, conv_id: str, content: str) -> Path:
        """Write a conversation document to the vault.

        Creates parent directories if needed. Returns the file path.
        """
        filepath = self._safe_path(user_id, conv_id)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        mtime = filepath.stat().st_mtime
        cache_key = f"{user_id}/{conv_id}"
        self._cache[cache_key] = (mtime, content)
        logger.info(
            f"Wrote conversation doc {conv_id} for user {user_id} ({len(content)} chars)"
        )
        return filepath

    def read_doc(self, user_id: str, conv_id: str) -> str:
        """Read a conversation document from the vault.

        Returns empty string if the file doesn't exist.
        """
        filepath = self._safe_path(user_id, conv_id)
        cache_key = f"{user_id}/{conv_id}"
        try:
            if not filepath.exists():
                return ""
            mtime = filepath.stat().st_mtime
            cached = self._cache.get(cache_key)
            if cached and cached[0] == mtime:
                return cached[1]
            content = filepath.read_text(encoding="utf-8")
            self._cache[cache_key] = (mtime, content)
            return content
        except Exception as e:
            logger.warning(f"Failed to read doc {conv_id} for user {user_id}: {e}")
            return ""

    def delete_doc(self, user_id: str, conv_id: str) -> bool:
        """Delete a conversation document. Returns True if deleted."""
        filepath = self._safe_path(user_id, conv_id)
        cache_key = f"{user_id}/{conv_id}"
        try:
            if filepath.exists():
                filepath.unlink()
                self._cache.pop(cache_key, None)
                logger.info(f"Deleted conversation doc {conv_id} for user {user_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete doc {conv_id} for user {user_id}: {e}")
            return False

    def list_docs(self, user_id: str) -> List[str]:
        """List all conversation IDs for a user."""
        safe_uid = Path(user_id).name
        user_dir = self._base_dir / safe_uid
        if not user_dir.exists():
            return []
        return [p.stem for p in user_dir.glob("*.md")]

    def delete_all_docs(self, user_id: str) -> int:
        """Delete all conversation documents for a user. Returns count deleted."""
        safe_uid = Path(user_id).name
        user_dir = self._base_dir / safe_uid
        if not user_dir.exists():
            return 0

        count = 0
        for p in user_dir.glob("*.md"):
            try:
                p.unlink()
                cache_key = f"{user_id}/{p.stem}"
                self._cache.pop(cache_key, None)
                count += 1
            except Exception as e:
                logger.error(f"Failed to delete {p}: {e}")

        # Remove empty user directory
        try:
            if user_dir.exists() and not any(user_dir.iterdir()):
                user_dir.rmdir()
        except Exception:
            pass

        logger.info(f"Deleted {count} conversation docs for user {user_id}")
        return count
