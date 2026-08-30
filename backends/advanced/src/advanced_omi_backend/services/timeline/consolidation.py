"""Vision-assisted, non-mutating merge suggestions for one reviewed day."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.timeline import (
    TimelineDay,
    TimelineEpisode,
    TimelineReviewDecision,
    TimelineSemanticGroup,
    utcnow,
)
from advanced_omi_backend.services.memory.visibility import conversation_scope_filter
from advanced_omi_backend.services.timeline.merge_synthesis import (
    synthesize_merged_episode_account,
)

logger = logging.getLogger(__name__)


class ConsolidationSuggestion(BaseModel):
    suggestion_id: str
    episode_ids: list[str] = Field(min_length=2)
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class ConsolidationResolutionError(RuntimeError):
    """The grouping proposal changed before the person's decision landed."""


def suggestions_match_active_episodes(
    suggestions: Iterable[dict[str, Any]], active_episode_ids: set[str]
) -> bool:
    """Whether every proposed merge still names its complete active membership."""

    return all(
        len(item.get("episode_ids") or []) >= 2
        and set(item.get("episode_ids") or []) <= active_episode_ids
        for item in suggestions
    )


def active_semantic_groups(day: TimelineDay) -> list[TimelineSemanticGroup]:
    """Return only groups belonging to the day generation currently on screen."""

    if not day.active_run_id:
        return []
    return [group for group in day.semantic_groups if group.run_id == day.active_run_id]


async def resolve_day_consolidation(
    day: TimelineDay, accepted_suggestion_ids: Iterable[str]
) -> list[TimelineSemanticGroup]:
    """Persist accepted overlays and every accept/reject decision atomically.

    Synthesis completes before the compare-and-set write.  A failure therefore leaves
    the day, its proposal, and all episode claims unchanged.
    """

    if day.consolidation_state != "ready" or not day.active_run_id:
        raise ConsolidationResolutionError(
            "This grouping proposal is no longer awaiting review"
        )
    suggestions = [
        ConsolidationSuggestion.model_validate(item)
        for item in day.consolidation_suggestions
    ]
    known = {item.suggestion_id for item in suggestions}
    accepted_ids = set(accepted_suggestion_ids)
    if not accepted_ids <= known:
        raise ConsolidationResolutionError(
            "The decision includes a grouping outside this proposal"
        )

    episodes = (
        await TimelineEpisode.find(
            TimelineEpisode.user_id == day.user_id,
            TimelineEpisode.run_id == day.active_run_id,
            {"status": {"$ne": "superseded"}},
        )
        .sort("started_at")
        .to_list()
    )
    episode_map = {episode.episode_id: episode for episode in episodes}
    if not suggestions_match_active_episodes(
        day.consolidation_suggestions, set(episode_map)
    ):
        raise ConsolidationResolutionError(
            "Episodes changed after this grouping proposal was generated"
        )

    accepted = [item for item in suggestions if item.suggestion_id in accepted_ids]
    already_grouped = {
        episode_id
        for group in active_semantic_groups(day)
        for episode_id in group.episode_ids
    }
    if any(already_grouped & set(item.episode_ids) for item in accepted):
        raise ConsolidationResolutionError(
            "Remove the existing semantic group before accepting this proposal"
        )
    accounts = await asyncio.gather(
        *(
            synthesize_merged_episode_account(
                [episode_map[episode_id] for episode_id in item.episode_ids]
            )
            for item in accepted
        )
    )
    now = utcnow()
    groups: list[TimelineSemanticGroup] = []
    for item, account in zip(accepted, accounts):
        members = [episode_map[episode_id] for episode_id in item.episode_ids]
        groups.append(
            TimelineSemanticGroup(
                run_id=day.active_run_id,
                episode_ids=item.episode_ids,
                episode_keys=[episode.episode_key for episode in members],
                title=account.title,
                summary=account.summary,
                started_at=min(_utc(episode.started_at) for episode in members),
                ended_at=max(_utc(episode.ended_at) for episode in members),
                suggestion_id=item.suggestion_id,
                reason=item.reason,
                confidence=item.confidence,
                model=day.consolidation_model,
                created_at=now,
            )
        )

    group_by_suggestion = {group.suggestion_id: group for group in groups}
    decisions = [
        TimelineReviewDecision(
            run_id=day.active_run_id,
            action=(
                "grouping_accept"
                if item.suggestion_id in accepted_ids
                else "grouping_reject"
            ),
            episode_ids=item.episode_ids,
            suggestion_id=item.suggestion_id,
            model=day.consolidation_model,
            before=item.model_dump(mode="json"),
            after=(
                group_by_suggestion[item.suggestion_id].model_dump(mode="json")
                if item.suggestion_id in group_by_suggestion
                else {"kept_separate": True}
            ),
            created_at=now,
        )
        for item in suggestions
    ]
    update: dict[str, Any] = {
        "$set": {
            "consolidation_state": "resolved",
            "consolidation_resolved_at": now,
            "consolidation_error": None,
            "revised_at": now,
        },
        "$push": {
            "review_decisions": {
                "$each": [item.model_dump(mode="python") for item in decisions]
            }
        },
    }
    if groups:
        update["$push"]["semantic_groups"] = {
            "$each": [group.model_dump(mode="python") for group in groups]
        }
    result = await TimelineDay.get_pymongo_collection().update_one(
        {
            "_id": day.id,
            "active_run_id": day.active_run_id,
            "consolidation_state": "ready",
        },
        update,
    )
    if not result.modified_count:
        raise ConsolidationResolutionError(
            "This grouping proposal changed before the decision could be saved"
        )
    return groups


async def create_manual_semantic_group(
    day: TimelineDay, episode_ids: Iterable[str]
) -> TimelineSemanticGroup:
    """Create one reviewed overlay without changing any member episode."""

    ids = list(dict.fromkeys(episode_ids))
    if len(ids) < 2 or not day.active_run_id:
        raise ConsolidationResolutionError("Select at least two active episodes")
    episodes = await TimelineEpisode.find(
        TimelineEpisode.user_id == day.user_id,
        TimelineEpisode.run_id == day.active_run_id,
        {"episode_id": {"$in": ids}, "status": {"$ne": "superseded"}},
    ).to_list()
    episode_map = {episode.episode_id: episode for episode in episodes}
    if set(ids) != set(episode_map):
        raise ConsolidationResolutionError("One or more selected episodes changed")
    grouped_ids = {
        episode_id
        for group in active_semantic_groups(day)
        for episode_id in group.episode_ids
    }
    if grouped_ids & set(ids):
        raise ConsolidationResolutionError(
            "Remove the existing semantic group before regrouping its episodes"
        )
    members = sorted(
        (episode_map[episode_id] for episode_id in ids),
        key=lambda item: _utc(item.started_at),
    )
    account = await synthesize_merged_episode_account(members)
    now = utcnow()
    proposal_items = (
        [
            ConsolidationSuggestion.model_validate(item)
            for item in day.consolidation_suggestions
        ]
        if day.consolidation_state == "ready"
        else []
    )
    matched = next(
        (item for item in proposal_items if set(item.episode_ids) == set(ids)), None
    )
    group = TimelineSemanticGroup(
        run_id=day.active_run_id,
        episode_ids=[episode.episode_id for episode in members],
        episode_keys=[episode.episode_key for episode in members],
        title=account.title,
        summary=account.summary,
        started_at=min(_utc(episode.started_at) for episode in members),
        ended_at=max(_utc(episode.ended_at) for episode in members),
        suggestion_id=matched.suggestion_id if matched else None,
        reason=(
            matched.reason if matched else "Manually grouped during Timeline review"
        ),
        confidence=matched.confidence if matched else None,
        model=day.consolidation_model if matched else None,
        created_at=now,
    )
    decisions = [
        TimelineReviewDecision(
            run_id=day.active_run_id,
            action="grouping_accept",
            episode_ids=group.episode_ids,
            suggestion_id=group.suggestion_id,
            model=group.model,
            before=(
                matched.model_dump(mode="json")
                if matched
                else {"source": "manual", "episode_ids": group.episode_ids}
            ),
            after=group.model_dump(mode="json"),
            created_at=now,
        )
    ]
    decisions.extend(
        TimelineReviewDecision(
            run_id=day.active_run_id,
            action="grouping_reject",
            episode_ids=item.episode_ids,
            suggestion_id=item.suggestion_id,
            model=day.consolidation_model,
            before=item.model_dump(mode="json"),
            after={"kept_separate": True, "resolved_by": "manual_grouping"},
            created_at=now,
        )
        for item in proposal_items
        if matched is None or item.suggestion_id != matched.suggestion_id
    )
    set_fields: dict[str, Any] = {"revised_at": now}
    if proposal_items:
        set_fields.update(
            consolidation_state="resolved",
            consolidation_resolved_at=now,
            consolidation_error=None,
        )
    result = await TimelineDay.get_pymongo_collection().update_one(
        {
            "_id": day.id,
            "active_run_id": day.active_run_id,
            "review_state": "episodes_pending",
        },
        {
            "$push": {
                "semantic_groups": group.model_dump(mode="python"),
                "review_decisions": {
                    "$each": [item.model_dump(mode="python") for item in decisions]
                },
            },
            "$set": set_fields,
        },
    )
    if not result.modified_count:
        raise ConsolidationResolutionError(
            "The day changed before the semantic group could be saved"
        )
    return group


async def remove_semantic_group(day: TimelineDay, group_id: str) -> None:
    """Remove an active overlay while retaining the decision history."""

    group = next(
        (item for item in active_semantic_groups(day) if item.group_id == group_id),
        None,
    )
    if group is None or not day.active_run_id:
        raise ConsolidationResolutionError("Semantic group not found")
    now = utcnow()
    decision = TimelineReviewDecision(
        run_id=day.active_run_id,
        action="grouping_remove",
        episode_ids=group.episode_ids,
        suggestion_id=group.suggestion_id,
        model=group.model,
        before=group.model_dump(mode="json"),
        after={"removed": True},
        created_at=now,
    )
    result = await TimelineDay.get_pymongo_collection().update_one(
        {
            "_id": day.id,
            "active_run_id": day.active_run_id,
            "review_state": "episodes_pending",
            "semantic_groups.group_id": group_id,
        },
        {
            "$pull": {"semantic_groups": {"group_id": group_id}},
            "$push": {"review_decisions": decision.model_dump(mode="python")},
            "$set": {"revised_at": now},
        },
    )
    if not result.modified_count:
        raise ConsolidationResolutionError(
            "The day changed before the semantic group could be removed"
        )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def render_day_tape_png(episodes: Iterable[Any], timezone_name: str) -> bytes:
    """Render a legible local-time tape, rasterized by Chronicle's ffmpeg runtime."""

    items = sorted(
        episodes, key=lambda item: (_utc(item.started_at), _utc(item.ended_at))
    )
    zone = ZoneInfo(timezone_name)
    width, row_height = 1600, 68
    height = max(420, 116 + len(items) * row_height)
    axis_left, axis_width = 1080, 470
    rows: list[str] = []
    previous_end: datetime | None = None
    for index, item in enumerate(items, 1):
        y = 94 + (index - 1) * row_height
        start, end = _utc(item.started_at).astimezone(zone), _utc(
            item.ended_at
        ).astimezone(zone)
        minute = start.hour * 60 + start.minute + start.second / 60
        duration = max(1.0, (end - start).total_seconds() / 60)
        x = axis_left + round(axis_width * minute / 1440)
        bar_width = max(8, round(axis_width * duration / 1440))
        lane = (
            "conversation"
            if getattr(item, "conversational", False)
            else getattr(item, "activity_mode", "foreground")
        )
        color = {"conversation": "#b45b30", "background": "#69645b"}.get(
            lane, "#c6b8a3"
        )
        title = html.escape(str(getattr(item, "title", ""))[:74])
        gap = ""
        if previous_end is not None:
            gap_minutes = max(0, round((start - previous_end).total_seconds() / 60))
            gap = f'<text x="970" y="{y - 17}" text-anchor="end" class="gap">gap {gap_minutes}m</text>'
        rows.append(
            f'{gap}<text x="30" y="{y + 18}" class="label">E{index:02d}</text>'
            f'<text x="100" y="{y + 18}" class="time">{start:%H:%M}–{end:%H:%M}</text>'
            f'<text x="270" y="{y + 18}" class="title">{title}</text>'
            f'<line x1="{axis_left}" y1="{y + 11}" x2="{axis_left + axis_width}" y2="{y + 11}" class="guide"/>'
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="22" rx="3" fill="{color}"/>'
        )
        previous_end = end
    ticks = []
    for hour in (0, 6, 12, 18, 24):
        x = axis_left + round(axis_width * hour / 24)
        ticks.append(
            f'<line x1="{x}" y1="52" x2="{x}" y2="72" class="tick"/><text x="{x}" y="40" text-anchor="middle" class="axis">{hour:02d}</text>'
        )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
<rect width="100%" height="100%" fill="#1e1b17"/>
<style>.label{{fill:#e2dacf;font:700 22px DejaVu Sans Mono}}.time{{fill:#a99d8e;font:18px DejaVu Sans Mono}}.title{{fill:#e2dacf;font:19px DejaVu Sans}}.gap{{fill:#8d8173;font:14px DejaVu Sans}}.axis{{fill:#978a7d;font:15px DejaVu Sans Mono}}.tick{{stroke:#746759}}.guide{{stroke:#3e3831}}</style>
<text x="30" y="42" class="label">DAY TAPE · {html.escape(timezone_name)}</text>
<line x1="{axis_left}" y1="62" x2="{axis_left + axis_width}" y2="62" class="tick"/>{''.join(ticks)}{''.join(rows)}</svg>"""
    with tempfile.TemporaryDirectory(prefix="chronicle-day-tape-") as temp_dir:
        source, target = Path(temp_dir) / "day.svg", Path(temp_dir) / "day.png"
        source.write_text(svg, encoding="utf-8")
        completed = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-frames:v",
                "1",
                str(target),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or not target.exists():
            raise RuntimeError(
                f"day tape rendering failed: {completed.stderr.decode(errors='replace')[-500:]}"
            )
        return target.read_bytes()


def _validated_suggestions(
    raw: Any, episodes: list[Any]
) -> list[ConsolidationSuggestion]:
    ordered = sorted(
        episodes, key=lambda item: (_utc(item.started_at), _utc(item.ended_at))
    )
    labels = {f"E{index:02d}": item for index, item in enumerate(ordered, 1)}
    positions = {item.episode_id: index for index, item in enumerate(ordered)}
    used: set[str] = set()
    result: list[ConsolidationSuggestion] = []
    for candidate in raw.get("suggestions", []) if isinstance(raw, dict) else []:
        ids = [
            labels[label].episode_id
            for label in candidate.get("episode_labels", [])
            if label in labels
        ]
        ids = list(dict.fromkeys(ids))
        if len(ids) < 2 or any(item in used for item in ids):
            continue
        indices = sorted(positions[item] for item in ids)
        if indices != list(range(indices[0], indices[-1] + 1)):
            continue
        title = str(candidate.get("title") or "").strip()
        reason = str(candidate.get("reason") or "").strip()
        if not title or not reason:
            continue
        if len(reason) > 500:
            clipped = reason[:497]
            sentence_end = max(clipped.rfind(". "), clipped.rfind("; "))
            reason = (
                clipped[: sentence_end + 1]
                if sentence_end >= 180
                else clipped.rsplit(" ", 1)[0] + "…"
            )
        confidence = max(0.0, min(1.0, float(candidate.get("confidence", 0))))
        digest = hashlib.sha1("\0".join(ids).encode()).hexdigest()[:12]  # noqa: S324
        result.append(
            ConsolidationSuggestion(
                suggestion_id=f"group:{digest}",
                episode_ids=ids,
                title=title[:160],
                reason=reason,
                confidence=confidence,
            )
        )
        used.update(ids)
    return result


def _parse_response_object(content: str) -> dict[str, Any] | None:
    """Parse a JSON object even when a model wraps it in prose or a code fence."""

    text = (content or "").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def suggest_episode_consolidation(
    episodes: list[Any], transcripts: dict[str, str], timezone_name: str
) -> dict[str, Any]:
    ordered = sorted(
        episodes, key=lambda item: (_utc(item.started_at), _utc(item.ended_at))
    )
    if len(ordered) < 2:
        image = await asyncio.to_thread(render_day_tape_png, ordered, timezone_name)
        return {"image": image, "suggestions": [], "model": None}
    registry = get_models_registry()
    if registry is None:
        raise RuntimeError("model registry is unavailable")
    operation = registry.get_llm_operation("timeline_consolidation")
    if "vision" not in operation.model_def.capabilities:
        raise RuntimeError(
            f"model {operation.model_def.name} is not configured for vision"
        )
    image = await asyncio.to_thread(render_day_tape_png, ordered, timezone_name)
    payload = []
    for index, episode in enumerate(ordered, 1):
        transcript = "\n".join(
            transcripts.get(cid, "") for cid in episode.related_conversation_ids
        )
        payload.append(
            {
                "label": f"E{index:02d}",
                "started_at": _utc(episode.started_at).isoformat(),
                "ended_at": _utc(episode.ended_at).isoformat(),
                "title": episode.title,
                "summary": episode.summary,
                "kind": episode.kind,
                "entities": episode.entities,
                "transcript_excerpt": transcript[:3500],
            }
        )
    prompt = """Review this Chronicle day for over-fragmentation. The image is a canonical day tape: each row is labelled E01, E02, and so on, and its bar is positioned on a 24-hour axis. Use the structured episode evidence below for exact meaning.

An episode is an exact bounded evidence claim. A semantic group says that several distinct episodes belong to the same real-world activity, conversation, debugging effort, meeting, journey, or goal; it does not erase the gaps or widen their captured intervals. Propose a group when consecutive episodes have that shared meaning, even if capture stopped temporarily. Keep them separate when purpose, participants, setting, or activity meaningfully changes. When uncertain, do not suggest a group.

Return JSON only: {"suggestions":[{"episode_labels":["E01","E02"],"title":"...","reason":"...","confidence":0.0}]}. Suggestions must not overlap and each group must be consecutive in the supplied order.

Episodes:\n""" + json.dumps(
        payload, ensure_ascii=False
    )
    messages = operation.prepare_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(image).decode()
                        },
                    },
                ],
            }
        ]
    )
    client = operation.get_client(is_async=True)
    raw: dict[str, Any] = {}
    for attempt in range(2):
        request_messages = list(messages)
        if attempt:
            request_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous object omitted the required suggestions array. "
                        "Review the supplied day again and return the exact requested "
                        "JSON shape, using an empty array only when no consecutive "
                        "episodes continue the same real-world objective."
                    ),
                }
            )
        response = await client.chat.completions.create(
            messages=request_messages, **operation.to_api_params()
        )
        content = response.choices[0].message.content or "{}"
        raw = _parse_response_object(content) or {}
        if isinstance(raw.get("suggestions"), list):
            break
    if not isinstance(raw.get("suggestions"), list):
        raise RuntimeError(
            "Timeline grouping model omitted the required suggestions array after 2 attempts"
        )
    logger.info("Timeline consolidation raw proposal: %s", raw)
    return {
        "image": image,
        "suggestions": [
            item.model_dump(mode="json")
            for item in _validated_suggestions(raw, ordered)
        ],
        "model": operation.model_def.name,
    }


async def generate_day_consolidation(
    user_id: str, local_date: Any, timezone_name: str, run_id: str
) -> dict[str, Any]:
    """Generate and persist a proposal only while ``run_id`` remains active."""

    collection = TimelineDay.get_pymongo_collection()
    fence = {
        "user_id": user_id,
        "local_date": datetime.combine(local_date, datetime.min.time()),
        "timezone": timezone_name,
        "active_run_id": run_id,
        "review_state": "episodes_pending",
    }
    claimed = await collection.update_one(
        {**fence, "consolidation_state": {"$in": ["", "queued", "failed"]}},
        {
            "$set": {
                "consolidation_state": "generating",
                "consolidation_error": None,
                "consolidation_started_at": utcnow(),
                "consolidation_resolved_at": None,
            }
        },
    )
    if not claimed.modified_count:
        day = await collection.find_one(fence, {"consolidation_state": 1})
        return {"state": (day or {}).get("consolidation_state", "stale")}
    try:
        episodes = (
            await TimelineEpisode.find(
                TimelineEpisode.user_id == user_id,
                TimelineEpisode.run_id == run_id,
                {"status": {"$ne": "superseded"}},
            )
            .sort("started_at")
            .to_list()
        )
        conversation_ids = list(
            dict.fromkeys(
                cid for episode in episodes for cid in episode.related_conversation_ids
            )
        )
        conversations = (
            await Conversation.find(
                Conversation.user_id == user_id,
                conversation_scope_filter(),
                {"conversation_id": {"$in": conversation_ids}},
            ).to_list()
            if conversation_ids
            else []
        )
        result = await suggest_episode_consolidation(
            episodes,
            {item.conversation_id: item.transcript or "" for item in conversations},
            timezone_name,
        )
        generated_at = utcnow()
        saved = await collection.update_one(
            {**fence, "consolidation_state": "generating"},
            {
                "$set": {
                    "consolidation_state": "ready",
                    "consolidation_run_id": run_id,
                    "consolidation_model": result["model"],
                    "consolidation_suggestions": result["suggestions"],
                    "consolidation_generated_at": generated_at,
                    "consolidation_resolved_at": None,
                    "consolidation_started_at": None,
                    "consolidation_error": None,
                }
            },
        )
        return {
            "state": "ready" if saved.modified_count else "stale",
            "run_id": run_id,
            "model": result["model"],
            "suggestions": result["suggestions"],
            "generated_at": generated_at,
        }
    except Exception as error:
        await collection.update_one(
            {**fence, "consolidation_state": "generating"},
            {
                "$set": {
                    "consolidation_state": "failed",
                    "consolidation_error": str(error)[:1000],
                    "consolidation_started_at": None,
                }
            },
        )
        raise


async def queue_day_consolidation(
    user_id: str, local_date: Any, timezone_name: str, run_id: str
) -> dict[str, str]:
    """Enqueue grouping inference so the HTTP request is only an acknowledgement."""

    # Lazy imports avoid the queue_controller -> timeline worker import cycle.
    from advanced_omi_backend.controllers.queue_controller import default_queue
    from advanced_omi_backend.services.timeline.executor import settings_dict
    from advanced_omi_backend.workers.timeline_jobs import (
        generate_timeline_consolidation_job,
    )

    settings = settings_dict().get("consolidation") or {}
    default_queue.enqueue(
        generate_timeline_consolidation_job,
        user_id,
        local_date.isoformat(),
        timezone_name,
        run_id,
        job_timeout=int(settings.get("timeout_seconds", 300)),
    )
    return {
        "state": "queued",
        "run_id": run_id,
        "model": None,
        "suggestions": [],
        "error": None,
        "generated_at": None,
    }


async def prefetch_consolidation_horizon() -> dict[str, int]:
    """Keep grouping proposals ready for the oldest reviewable days per user."""

    # Lazy imports avoid the queue_controller -> timeline worker import cycle.
    from advanced_omi_backend.controllers.queue_controller import default_queue
    from advanced_omi_backend.services.timeline.executor import settings_dict
    from advanced_omi_backend.workers.timeline_jobs import (
        generate_timeline_consolidation_job,
    )

    settings = settings_dict().get("consolidation") or {}
    if not settings.get("pregenerate", True):
        return {"users": 0, "considered": 0, "queued": 0, "failed": 0}
    horizon = max(1, min(30, int(settings.get("prefetch_days", 5))))
    timeout = int(settings.get("timeout_seconds", 300))
    collection = TimelineDay.get_pymongo_collection()
    users = await collection.distinct(
        "user_id",
        {"active_run_id": {"$nin": [None, ""]}, "review_state": "episodes_pending"},
    )
    totals = {"users": len(users), "considered": 0, "queued": 0, "failed": 0}
    for user_id in users:
        days = (
            await collection.find(
                {
                    "user_id": user_id,
                    "active_run_id": {"$nin": [None, ""]},
                    "review_state": "episodes_pending",
                },
                {
                    "local_date": 1,
                    "timezone": 1,
                    "active_run_id": 1,
                    "consolidation_state": 1,
                    "consolidation_suggestions": 1,
                    "consolidation_started_at": 1,
                },
            )
            .sort("local_date", 1)
            .limit(horizon)
            .to_list(length=horizon)
        )
        totals["considered"] += len(days)
        for day in days:
            state = day.get("consolidation_state")
            started_at = day.get("consolidation_started_at")
            expired = state in {"queued", "generating"} and (
                started_at is None
                or utcnow() - _utc(started_at) > timedelta(seconds=timeout + 30)
            )
            if expired:
                await collection.update_one(
                    {
                        "_id": day["_id"],
                        "active_run_id": day["active_run_id"],
                        "consolidation_state": state,
                    },
                    {
                        "$set": {
                            "consolidation_state": "",
                            "consolidation_started_at": None,
                            "consolidation_error": "Grouping generation timed out and was requeued",
                        }
                    },
                )
                state = ""
            if state == "ready" and day.get("consolidation_suggestions"):
                active_ids = set(
                    await TimelineEpisode.get_pymongo_collection().distinct(
                        "episode_id",
                        {
                            "user_id": user_id,
                            "run_id": day["active_run_id"],
                            "status": {"$ne": "superseded"},
                        },
                    )
                )
                if not suggestions_match_active_episodes(
                    day["consolidation_suggestions"], active_ids
                ):
                    await collection.update_one(
                        {"_id": day["_id"], "active_run_id": day["active_run_id"]},
                        {
                            "$set": {
                                "consolidation_state": "",
                                "consolidation_suggestions": [],
                                "consolidation_error": None,
                            }
                        },
                    )
                    state = ""
            if state in {"queued", "generating", "ready", "resolved"}:
                continue
            claimed = await collection.update_one(
                {
                    "_id": day["_id"],
                    "active_run_id": day["active_run_id"],
                    "review_state": "episodes_pending",
                    "consolidation_state": {"$in": [None, "", "failed"]},
                },
                {
                    "$set": {
                        "consolidation_state": "queued",
                        "consolidation_error": None,
                        "consolidation_started_at": utcnow(),
                    }
                },
            )
            if not claimed.modified_count:
                continue
            try:
                default_queue.enqueue(
                    generate_timeline_consolidation_job,
                    user_id,
                    day["local_date"].date().isoformat(),
                    day["timezone"],
                    day["active_run_id"],
                    job_timeout=timeout,
                )
                totals["queued"] += 1
            except Exception as error:
                totals["failed"] += 1
                await collection.update_one(
                    {"_id": day["_id"], "active_run_id": day["active_run_id"]},
                    {
                        "$set": {
                            "consolidation_state": "failed",
                            "consolidation_error": str(error)[:1000],
                            "consolidation_started_at": None,
                        }
                    },
                )
                logger.exception("Could not prefetch consolidation for %s", day["_id"])
    return totals
