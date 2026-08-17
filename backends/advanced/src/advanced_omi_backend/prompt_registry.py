"""Centralized prompt registry backed by LangFuse.

Stores default prompts registered at startup and resolves overrides from
LangFuse's prompt management. Falls back to defaults when LangFuse is
unavailable. Admin prompt editing is handled via the LangFuse web UI.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from langfuse import Langfuse

logger = logging.getLogger(__name__)


class PromptRegistry:
    """Registry that holds default prompts and resolves overrides from LangFuse."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 60.0,
        failure_cooldown_seconds: float = 300.0,
    ):
        self._defaults: Dict[str, str] = {}  # prompt_id -> default template text
        self._langfuse = None  # Lazy-init LangFuse client
        self._client_initialized = False
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._failure_cooldown_seconds = max(0.0, float(failure_cooldown_seconds))
        self._remote_prompts: Dict[str, tuple[float, Any]] = {}
        self._remote_unavailable_until: Dict[str, float] = {}
        self._fetch_lock = asyncio.Lock()

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
        if self._client_initialized:
            return self._langfuse
        self._client_initialized = True
        try:
            self._langfuse = Langfuse()
        except Exception as e:
            logger.warning(f"LangFuse client init failed: {e}")
        return self._langfuse

    @staticmethod
    def _compile_remote(prompt_obj: Any, variables: dict[str, Any]) -> str:
        if variables:
            return prompt_obj.compile(**variables)
        return prompt_obj.compile()

    def _compile_default(self, prompt_id: str, variables: dict[str, Any]) -> str:
        template_text = self._defaults.get(prompt_id)
        if template_text is None:
            raise KeyError(f"Unknown prompt_id: {prompt_id}")
        for key, value in variables.items():
            template_text = template_text.replace(f"{{{{{key}}}}}", str(value))
        return template_text

    async def get_prompt(self, prompt_id: str, **variables) -> str:
        """Return prompt text from LangFuse with fallback to default.

        If ``variables`` are provided, ``{{var}}`` placeholders are
        compiled automatically (LangFuse SDK or manual substitution).
        """
        now = time.monotonic()
        cached = self._remote_prompts.get(prompt_id)
        if cached is not None and cached[0] > now:
            return self._compile_remote(cached[1], variables)
        if now < self._remote_unavailable_until.get(prompt_id, 0.0):
            return self._compile_default(prompt_id, variables)

        async with self._fetch_lock:
            now = time.monotonic()
            cached = self._remote_prompts.get(prompt_id)
            if cached is not None and cached[0] > now:
                return self._compile_remote(cached[1], variables)
            if now < self._remote_unavailable_until.get(prompt_id, 0.0):
                return self._compile_default(prompt_id, variables)
            try:
                client = self._get_client()
                if client is not None:
                    fallback = self._defaults.get(prompt_id, "")
                    prompt_obj = await asyncio.to_thread(
                        client.get_prompt,
                        prompt_id,
                        fallback=fallback,
                    )
                    self._remote_prompts[prompt_id] = (
                        now + self._cache_ttl_seconds,
                        prompt_obj,
                    )
                    if bool(getattr(prompt_obj, "is_fallback", False)):
                        self._remote_unavailable_until[prompt_id] = (
                            now + self._failure_cooldown_seconds
                        )
                    else:
                        self._remote_unavailable_until.pop(prompt_id, None)
                    return self._compile_remote(prompt_obj, variables)
            except Exception as e:
                self._remote_unavailable_until[prompt_id] = (
                    now + self._failure_cooldown_seconds
                )
                logger.debug(f"LangFuse prompt fetch failed for {prompt_id}: {e}")

        return self._compile_default(prompt_id, variables)

    async def seed_prompts(self) -> None:
        """Create or update prompts in LangFuse, skipping unchanged ones.

        Called once at startup after all defaults have been registered.
        Only creates a new version when the prompt text has actually changed.
        """
        client = self._get_client()
        if client is None:
            logger.info("LangFuse not available — skipping prompt seeding")
            return

        seeded = 0
        skipped = 0
        for prompt_id, template_text in self._defaults.items():
            try:
                # Check if the prompt already exists with the same text
                existing = None
                try:
                    existing = await asyncio.to_thread(client.get_prompt, prompt_id)
                except Exception:
                    pass  # Prompt doesn't exist yet

                if existing is not None:
                    existing_text = getattr(existing, "prompt", None)
                    if existing_text == template_text:
                        skipped += 1
                        continue

                await asyncio.to_thread(
                    client.create_prompt,
                    name=prompt_id,
                    type="text",
                    prompt=template_text,
                    labels=["production"],
                )
                self._remote_prompts.pop(prompt_id, None)
                seeded += 1
            except Exception as e:
                logger.warning(f"Failed to seed prompt '{prompt_id}': {e}")

        logger.info(f"Prompt seeding complete: {seeded} created, {skipped} unchanged")


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
