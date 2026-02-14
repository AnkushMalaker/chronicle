"""User-scoped prompt resolution and annotation-to-prompt mapping.

Provides ``get_user_prompt()`` which checks for a per-user prompt override
in LangFuse before falling back to the global prompt from the registry.
User-scoped prompts are created by the prompt optimization cron job that
analyzes user annotations and rewrites prompts to match user preferences.
"""

import logging
from typing import Optional

from advanced_omi_backend.models.annotation import AnnotationType
from advanced_omi_backend.prompt_registry import get_prompt_registry

logger = logging.getLogger(__name__)

# Maps annotation types to the prompts they optimize and the meta-optimizer
# prompt used to do the rewriting.
ANNOTATION_PROMPT_MAP = {
    AnnotationType.TITLE: {
        "target_prompt": "conversation.title_summary",
        "optimizer_prompt": "prompt_optimization.title_optimizer",
    },
    AnnotationType.MEMORY: {
        "target_prompt": "memory.fact_retrieval",
        "optimizer_prompt": "prompt_optimization.memory_optimizer",
    },
}


async def get_user_prompt(
    prompt_id: str,
    user_id: Optional[str] = None,
    **variables,
) -> str:
    """Resolve a prompt with optional per-user override from LangFuse.

    Resolution order (first match wins):
    1. LangFuse user-scoped prompt  ``{prompt_id}:user:{user_id}``
    2. Global prompt via ``registry.get_prompt(prompt_id)``

    Falls back gracefully on any error (LangFuse unavailable, prompt not
    found, etc.) so callers always get a usable prompt string.

    Args:
        prompt_id: Dotted prompt identifier (e.g. "conversation.title_summary")
        user_id: Optional user ID for per-user override lookup
        **variables: Template variables to compile into the prompt

    Returns:
        Compiled prompt text ready for LLM consumption
    """
    registry = get_prompt_registry()

    # Try user-scoped override when user_id is provided
    if user_id:
        user_prompt_name = f"{prompt_id}:user:{user_id}"
        try:
            client = registry._get_client()
            if client is not None:
                prompt_obj = client.get_prompt(user_prompt_name)
                if variables:
                    return prompt_obj.compile(**variables)
                return prompt_obj.compile()
        except Exception:
            # User-scoped prompt not found or LangFuse unavailable — fall through
            logger.debug(
                f"No user-scoped prompt '{user_prompt_name}', falling back to global"
            )

    # Fall back to global prompt (LangFuse override or code default)
    return await registry.get_prompt(prompt_id, **variables)
