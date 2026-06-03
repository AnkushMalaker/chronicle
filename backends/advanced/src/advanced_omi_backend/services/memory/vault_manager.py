"""Conversation document vault manager.

Provides read/write access to per-user conversation document markdown files.
Layout: ``{base_dir}/{user_id}/{conversation_id}.md``

Same pattern as ``services/knowledge_graph/kb.py``: mtime-cache,
path-traversal guard, pure file I/O.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .vault_scaffold import SCAFFOLD_NOTE_NAMES

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

    def user_root(self, user_id: str) -> Path:
        """Return the per-user vault root (the directory the memory agent edits)."""
        return self._base_dir / Path(user_id).name

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
        """List all note paths for a user, relative to the user root.

        Recursive so it covers the agent's ``Conversations/``/``People/``/``Topics/``
        subfolders as well as the flat layout. Scaffold hub notes (``People.md`` etc.)
        are excluded — they are views, not captured content.
        """
        user_dir = self.user_root(user_id)
        if not user_dir.exists():
            return []
        return sorted(
            p.relative_to(user_dir).as_posix()
            for p in user_dir.rglob("*.md")
            if p.name not in SCAFFOLD_NOTE_NAMES
        )

    def delete_all_docs(self, user_id: str) -> int:
        """Delete all of a user's notes (recursively). Returns the count removed."""
        user_dir = self.user_root(user_id)
        if not user_dir.exists():
            return 0

        count = sum(1 for _ in user_dir.rglob("*.md"))
        try:
            shutil.rmtree(user_dir)
        except Exception as e:
            logger.error(f"Failed to delete vault for user {user_id}: {e}")
            return 0
        # Drop any cached docs for this user.
        self._cache = {
            k: v for k, v in self._cache.items() if not k.startswith(f"{user_id}/")
        }
        logger.info(f"Deleted {count} notes for user {user_id}")
        return count
