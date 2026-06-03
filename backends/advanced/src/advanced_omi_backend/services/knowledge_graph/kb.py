"""Knowledge Base Manager for per-user MEMORY.md files.

Provides read/write access to a per-user markdown file that serves as
the user's "basic memory" — always injected into LLM calls for chat
and memory extraction to provide persistent user context.

This module is intentionally free of FalkorDB or any heavy dependencies.
It performs pure file I/O with an mtime-based cache to avoid redundant reads.
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("knowledge_graph.kb")

# Default base directory for per-user memory files
_DEFAULT_BASE_DIR = Path(os.getenv("DATA_DIR", "/app/data")) / "memory_md"


class KnowledgeBaseManager:
    """Manages per-user MEMORY.md files on disk.

    File layout:
        {base_dir}/{user_id}/MEMORY.md

    Thread-safe for reads. Concurrent writes from different processes
    result in last-write-wins, which is acceptable for a user-edited file.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self._base_dir = base_dir or _DEFAULT_BASE_DIR
        # mtime cache: user_id -> (mtime, content)
        self._cache: Dict[str, Tuple[float, str]] = {}

    def _user_file(self, user_id: str) -> Path:
        """Get the MEMORY.md path for a user, with path-traversal guard."""
        safe_id = Path(user_id).name  # strips any directory components
        return self._base_dir / safe_id / "MEMORY.md"

    def get_basic_memory(self, user_id: str) -> str:
        """Read the user's MEMORY.md content.

        Returns the full file content, or empty string if the file
        doesn't exist or on any I/O error.
        """
        filepath = self._user_file(user_id)
        try:
            if not filepath.exists():
                return ""

            mtime = filepath.stat().st_mtime
            cached = self._cache.get(user_id)
            if cached and cached[0] == mtime:
                return cached[1]

            content = filepath.read_text(encoding="utf-8")
            self._cache[user_id] = (mtime, content)
            return content

        except Exception as e:
            logger.warning(f"Failed to read basic memory for user {user_id}: {e}")
            return ""

    def write_basic_memory(self, user_id: str, content: str) -> bool:
        """Write content to the user's MEMORY.md.

        Creates parent directories if needed. Updates the mtime cache
        on success.

        Returns True on success, False on error.
        """
        filepath = self._user_file(user_id)
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            # Update cache with new mtime
            mtime = filepath.stat().st_mtime
            self._cache[user_id] = (mtime, content)
            logger.info(f"Wrote basic memory for user {user_id} ({len(content)} chars)")
            return True
        except Exception as e:
            logger.error(f"Failed to write basic memory for user {user_id}: {e}")
            return False

    async def consolidate_basic_memory(self, user_id: str, memories: List[str]) -> str:
        """Consolidate extracted memory facts into a structured MEMORY.md.

        Fetches the consolidation prompt from the prompt registry,
        sends existing MEMORY.md + new facts to the LLM, and writes
        the result back to disk.

        Args:
            user_id: The user whose basic memory to consolidate.
            memories: List of extracted fact strings.

        Returns:
            The updated MEMORY.md content.

        Raises:
            Exception: If LLM call or file write fails.
        """
        from advanced_omi_backend.llm_client import get_llm_client
        from advanced_omi_backend.prompt_registry import get_prompt_registry

        existing = self.get_basic_memory(user_id)

        # Resolve prompt from registry (supports LangFuse overrides)
        registry = get_prompt_registry()
        system_prompt = await registry.get_prompt("memory.consolidate_basic_memory")

        # Build user message with both inputs
        facts_block = "\n".join(f"- {m}" for m in memories)
        user_content = (
            f"## Existing MEMORY.md\n\n{existing or '(empty — first run)'}\n\n"
            f"## Extracted Memories ({len(memories)} facts)\n\n{facts_block}"
        )

        from advanced_omi_backend.model_registry import get_models_registry

        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not initialized")
        op = registry.get_llm_operation("memory_update")
        client = op.get_client(is_async=False)
        raw_response = client.chat.completions.create(
            **op.to_api_params(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        response = raw_response.choices[0].message.content

        updated_content = response.strip()
        self.write_basic_memory(user_id, updated_content)
        logger.info(
            f"Consolidated basic memory for user {user_id}: "
            f"{len(memories)} facts → {len(updated_content)} chars"
        )
        return updated_content
