"""Cron job for annotation-driven prompt optimization.

Analyzes accumulated user corrections (title edits, memory edits) and uses a
meta-optimizer LLM to rewrite the target prompts.  Improved prompts are stored
in LangFuse as per-user overrides that ``get_user_prompt()`` resolves at
inference time.

Requires LangFuse to be configured (LANGFUSE_* env vars).  Logs a warning and
returns early if LangFuse is unavailable.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from advanced_omi_backend.llm_client import async_generate
from advanced_omi_backend.prompt_optimizer import ANNOTATION_PROMPT_MAP, get_user_prompt

logger = logging.getLogger(__name__)

# Minimum number of unprocessed annotations per user before running the optimizer
MIN_ANNOTATIONS = 3

# Marker appended to processed_by so we don't re-process the same annotations
_PROCESSED_MARKER = "prompt_optimization"


async def run_prompt_optimization_job() -> dict:
    """Analyze user annotations and optimize LLM prompts.

    For each annotation type in ``ANNOTATION_PROMPT_MAP``:
    1. Query accepted annotations not yet consumed by prompt_optimization.
    2. Group by ``user_id``.
    3. For each user with >= ``MIN_ANNOTATIONS`` corrections, call the
       meta-optimizer LLM to produce a revised prompt.
    4. Store the result in LangFuse as ``{prompt_id}:user:{user_id}``.
    5. Mark consumed annotations.

    Returns:
        Summary dict with counts of users/annotations processed.
    """
    from advanced_omi_backend.models.annotation import (
        Annotation,
        AnnotationStatus,
        AnnotationType,
    )
    from advanced_omi_backend.prompt_registry import get_prompt_registry

    registry = get_prompt_registry()
    langfuse_client = registry._get_client()

    if langfuse_client is None:
        logger.warning(
            "Prompt optimization: LangFuse not configured — skipping"
        )
        return {"skipped": True, "reason": "LangFuse not available"}

    total_users = 0
    total_annotations = 0
    errors = 0

    for ann_type, prompt_ids in ANNOTATION_PROMPT_MAP.items():
        target_prompt_id = prompt_ids["target_prompt"]
        optimizer_prompt_id = prompt_ids["optimizer_prompt"]

        # Fetch accepted annotations not yet consumed by this job
        all_annotations = await Annotation.find(
            Annotation.annotation_type == ann_type,
            Annotation.status == AnnotationStatus.ACCEPTED,
        ).to_list()

        unconsumed = [
            a
            for a in all_annotations
            if not a.processed_by or _PROCESSED_MARKER not in a.processed_by
        ]

        if not unconsumed:
            logger.info(
                f"Prompt optimization [{ann_type.value}]: no unprocessed annotations"
            )
            continue

        # Group by user_id
        by_user: Dict[str, List[Annotation]] = defaultdict(list)
        for ann in unconsumed:
            by_user[ann.user_id].append(ann)

        for user_id, annotations in by_user.items():
            if len(annotations) < MIN_ANNOTATIONS:
                logger.info(
                    f"Prompt optimization [{ann_type.value}]: user {user_id} "
                    f"has {len(annotations)} annotations (< {MIN_ANNOTATIONS}), skipping"
                )
                continue

            try:
                # Get current effective prompt for this user
                current_prompt = await get_user_prompt(
                    target_prompt_id,
                    user_id,
                )

                # Format corrections
                formatted = _format_corrections(annotations, ann_type)

                # Get the meta-optimizer prompt
                optimizer_prompt = await registry.get_prompt(
                    optimizer_prompt_id,
                    current_prompt=current_prompt,
                    count=str(len(annotations)),
                    formatted_corrections=formatted,
                )

                # Call meta-optimizer LLM
                response = await async_generate(
                    optimizer_prompt,
                    operation="prompt_optimization",
                )

                if not response:
                    logger.warning(
                        f"Prompt optimization [{ann_type.value}]: "
                        f"empty LLM response for user {user_id}"
                    )
                    errors += 1
                    continue

                # Parse response
                analysis, revised_prompt = _parse_optimizer_response(response)

                if not revised_prompt:
                    logger.warning(
                        f"Prompt optimization [{ann_type.value}]: "
                        f"could not parse revised prompt for user {user_id}"
                    )
                    errors += 1
                    continue

                # Store in LangFuse as user-scoped prompt
                user_prompt_name = f"{target_prompt_id}:user:{user_id}"
                try:
                    langfuse_client.create_prompt(
                        name=user_prompt_name,
                        type="text",
                        prompt=revised_prompt,
                        labels=["user-override"],
                        config={
                            "analysis": analysis,
                            "annotation_count": len(annotations),
                            "annotation_type": ann_type.value,
                            "optimized_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    logger.info(
                        f"Created new LangFuse prompt '{user_prompt_name}'"
                    )
                except Exception as e:
                    err_msg = str(e).lower()
                    if "already exists" in err_msg or "409" in err_msg:
                        # Prompt already exists — create a new version
                        langfuse_client.create_prompt(
                            name=user_prompt_name,
                            type="text",
                            prompt=revised_prompt,
                            labels=["user-override"],
                            config={
                                "analysis": analysis,
                                "annotation_count": len(annotations),
                                "annotation_type": ann_type.value,
                                "optimized_at": datetime.now(timezone.utc).isoformat(),
                            },
                            is_active=True,
                        )
                        logger.info(
                            f"Updated LangFuse prompt '{user_prompt_name}' (new version)"
                        )
                    else:
                        raise

                # Mark annotations as consumed
                for ann in annotations:
                    ann.processed_by = (
                        f"{ann.processed_by},{_PROCESSED_MARKER}"
                        if ann.processed_by
                        else _PROCESSED_MARKER
                    )
                    ann.updated_at = datetime.now(timezone.utc)
                    await ann.save()

                total_users += 1
                total_annotations += len(annotations)
                logger.info(
                    f"Prompt optimization [{ann_type.value}]: optimized for user {user_id} "
                    f"({len(annotations)} annotations)"
                )

            except Exception as e:
                logger.error(
                    f"Prompt optimization [{ann_type.value}]: "
                    f"error for user {user_id}: {e}",
                    exc_info=True,
                )
                errors += 1

    logger.info(
        f"Prompt optimization complete: {total_users} users, "
        f"{total_annotations} annotations processed, {errors} errors"
    )
    return {
        "users_optimized": total_users,
        "annotations_processed": total_annotations,
        "errors": errors,
    }


def _format_corrections(
    annotations: list,
    ann_type: "AnnotationType",
) -> str:
    """Format annotations as numbered correction examples for the meta-optimizer."""
    lines = []
    for i, ann in enumerate(annotations, 1):
        lines.append(f"{i}. Original: {ann.original_text}")
        lines.append(f"   Corrected: {ann.corrected_text}")
        lines.append("")
    return "\n".join(lines)


def _parse_optimizer_response(response: str) -> tuple:
    """Extract analysis and revised prompt from meta-optimizer output.

    Expected format::

        ANALYSIS:
        <text>

        REVISED_PROMPT:
        <text>

    Returns:
        (analysis_text, revised_prompt_text) — either may be empty string
    """
    analysis = ""
    revised_prompt = ""

    # Split on REVISED_PROMPT: marker
    match = re.split(r"REVISED_PROMPT:\s*\n?", response, maxsplit=1)
    if len(match) == 2:
        before_revised = match[0]
        revised_prompt = match[1].strip()

        # Extract ANALYSIS: from the first part
        analysis_match = re.split(r"ANALYSIS:\s*\n?", before_revised, maxsplit=1)
        if len(analysis_match) == 2:
            analysis = analysis_match[1].strip()

    return analysis, revised_prompt
