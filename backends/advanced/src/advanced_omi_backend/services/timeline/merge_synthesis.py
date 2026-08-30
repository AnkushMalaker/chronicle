"""Regenerate the semantic account after a human merges Timeline episodes."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from pydantic import BaseModel, Field

from advanced_omi_backend.llm_client import async_generate
from advanced_omi_backend.services.inference_artifacts import (
    load_reusable_result,
    persist_inference_run,
)

OPERATION = "timeline_episode_merge"
PROMPT_VERSION = "timeline-merge-v2"


class MergedEpisodeAccount(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    # Must match TimelineEpisode.summary so validation happens before any DB write.
    summary: str = Field(min_length=10, max_length=1200)


def _episode_source(episode: Any) -> dict[str, Any]:
    return {
        "started_at": episode.started_at,
        "ended_at": episode.ended_at,
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
        "entities": list(episode.entities),
        "claims": [
            (
                assertion.get("claim", "")
                if isinstance(assertion, dict)
                else assertion.claim
            )
            for assertion in episode.assertions
        ],
    }


def _json_object(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("merged episode synthesis must return a JSON object")
    return value


async def synthesize_merged_episode_account(
    episodes: Iterable[Any],
    *,
    force: bool = False,
) -> MergedEpisodeAccount:
    """Write one coherent title and summary from the selected episode accounts.

    This runs before any episode is superseded, so an inference failure leaves the
    existing Timeline untouched. Identical inputs reuse a durable inference artifact.
    """

    source = [_episode_source(episode) for episode in episodes]
    # Widening repeated slices with the same semantic account does not make that
    # account stale. This also keeps purely structural merges off the inference path.
    titles = {item["title"].strip() for item in source}
    summaries = {item["summary"].strip() for item in source}
    if not force and len(titles) == 1 and len(summaries) == 1:
        title = next(iter(titles))
        summary = next(iter(summaries))
        return MergedEpisodeAccount(
            title=title,
            summary=summary or f"Merged episode: {title}.",
        )
    request = {"prompt_version": PROMPT_VERSION, "episodes": source}
    cached = load_reusable_result(OPERATION, request)
    if cached is not None:
        return MergedEpisodeAccount.model_validate(cached)

    prompt = f"""You maintain a semantic personal timeline. A person selected the
following adjacent episode accounts and declared that they are one real-world event.
Write a fresh account of the whole merged event rather than preserving the first
segment's framing.

Return JSON with exactly:
- title: a concise, specific event-level title; identify an interview, meeting, call,
  journey, work session, or other event type when the evidence supports it
- summary: one coherent paragraph covering every materially distinct topic or outcome,
  no more than 1,000 characters

Do not invent facts. Do not call organizations or products conversation participants.
Prefer named human participants when they are supported by the supplied accounts.

EPISODES (chronological):
{json.dumps(source, ensure_ascii=False, default=str, indent=2)}
"""
    raw = await async_generate(prompt, operation="timeline_merge")
    account = MergedEpisodeAccount.model_validate(_json_object(raw))
    persist_inference_run(
        operation=OPERATION,
        request=request,
        stdout=raw,
        stderr="",
        result=account.model_dump(),
        metadata={"prompt_version": PROMPT_VERSION},
        reusable=True,
    )
    return account
