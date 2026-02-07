"""Centralized prompt registry backed by LangFuse.

Stores default prompts registered at startup and resolves overrides from
LangFuse's prompt management. Falls back to defaults when LangFuse is
unavailable. Admin prompt editing is handled via the LangFuse web UI.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PromptRegistry:
    """Registry that holds default prompts and resolves overrides from LangFuse."""

    def __init__(self):
        self._defaults: Dict[str, str] = {}  # prompt_id -> default template text
        self._langfuse = None  # Lazy-init LangFuse client

    def register_default(
        self,
        prompt_id: str,
        template: str,
        **kwargs,
    ) -> None:
        """Store a default prompt template for fallback and seeding.

        Extra keyword arguments (name, description, category, etc.) are
        accepted for backward compatibility but are not stored — LangFuse
        manages that metadata.
        """
        if prompt_id in self._defaults:
            logger.debug(f"Prompt '{prompt_id}' re-registered (overwriting default)")
        self._defaults[prompt_id] = template

    def _get_client(self):
        """Lazy-init LangFuse client (uses LANGFUSE_* env vars)."""
        if self._langfuse is None:
            try:
                from langfuse import Langfuse
                self._langfuse = Langfuse()
            except Exception as e:
                logger.warning(f"LangFuse client init failed: {e}")
                return None
        return self._langfuse

    async def get_prompt(self, prompt_id: str, **variables) -> str:
        """Return prompt text from LangFuse with fallback to default.

        If ``variables`` are provided, ``{{var}}`` placeholders are
        compiled automatically (LangFuse SDK or manual substitution).
        """
        template_text = None

        # Try LangFuse first
        try:
            client = self._get_client()
            if client is not None:
                fallback = self._defaults.get(prompt_id, "")
                prompt_obj = client.get_prompt(prompt_id, fallback=fallback)
                if variables:
                    return prompt_obj.compile(**variables)
                return prompt_obj.compile()
        except Exception as e:
            logger.debug(f"LangFuse prompt fetch failed for {prompt_id}: {e}")

        # Fallback to default
        template_text = self._defaults.get(prompt_id)
        if template_text is None:
            raise KeyError(f"Unknown prompt_id: {prompt_id}")

        if variables:
            for k, v in variables.items():
                template_text = template_text.replace(f"{{{{{k}}}}}", str(v))

        return template_text

    async def seed_prompts(self) -> None:
        """Create prompts in LangFuse if they don't already exist.

        Called once at startup after all defaults have been registered.
        """
        client = self._get_client()
        if client is None:
            logger.info("LangFuse not available — skipping prompt seeding")
            return

        seeded = 0
        skipped = 0
        for prompt_id, template_text in self._defaults.items():
            try:
                client.create_prompt(
                    name=prompt_id,
                    type="text",
                    prompt=template_text,
                    labels=["production"],
                )
                seeded += 1
            except Exception as e:
                err_msg = str(e).lower()
                if "already exists" in err_msg or "409" in err_msg:
                    skipped += 1
                else:
                    logger.warning(f"Failed to seed prompt '{prompt_id}': {e}")

        logger.info(f"Prompt seeding complete: {seeded} created, {skipped} already existed")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_registry: Optional[PromptRegistry] = None


def get_prompt_registry() -> PromptRegistry:
    """Get (or create) the global PromptRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = PromptRegistry()
    return _registry
