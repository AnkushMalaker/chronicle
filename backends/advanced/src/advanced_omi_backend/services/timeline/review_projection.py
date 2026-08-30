"""Presentation-only grouping for human Timeline review.

Timeline episodes remain the semantic source of truth.  This module turns one active
generation into a smaller set of chronological review groups; accepting, editing, or
deleting still targets the original episode ids.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PROJECTION_VERSION = "day-review-v2"
SESSION_GAP = timedelta(minutes=15)
MAX_SESSION_SPAN = timedelta(hours=2)
LOW_CONFIDENCE = 0.65


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _lane(episode: Any) -> str:
    if episode.conversational:
        return "conversation"
    if episode.activity_mode in {"background", "ambient", "idle"}:
        return "background"
    return "foreground"


def _episode_attention(episode: Any) -> list[str]:
    reasons: list[str] = []
    if episode.confidence < LOW_CONFIDENCE:
        reasons.append("low_confidence")
    if not episode.evidence_refs:
        reasons.append("missing_evidence")
    if episode.conversational and not episode.audio_ranges:
        reasons.append("missing_audio")
    if _utc(episode.ended_at) - _utc(episode.started_at) > timedelta(hours=2):
        reasons.append("long_episode")
    return reasons


def _dominant(values: Iterable[str], default: str) -> str:
    counts = Counter(value.strip() for value in values if value and value.strip())
    return counts.most_common(1)[0][0] if counts else default


def _group_title(episodes: list[Any], lane: str) -> str:
    # A manually merged episode already has the user's chosen semantic boundary and a
    # freshly synthesized account. Its title is more authoritative than guessing a
    # participant from an untyped entity bag (which once produced "with Amazon").
    if (
        len(episodes) == 1
        and episodes[0].confirmed_at is not None
        and episodes[0].title.strip()
    ):
        return episodes[0].title.strip()
    entities = [entity for episode in episodes for entity in episode.entities]
    entity = _dominant(entities, "")
    kind = _dominant((episode.kind for episode in episodes), "activity")
    if lane == "conversation" and entity:
        return f"Conversation with {entity}"
    if lane == "conversation":
        return "Conversation"
    if entity:
        return f"{kind.replace('_', ' ').title()} · {entity}"
    return kind.replace("_", " ").title()


def _episode_intervals(episode: Any) -> list[tuple[datetime, datetime]]:
    """Draw authoritative captured ranges when available, else the semantic bound."""

    ranges = [
        (_utc(item.started_at), _utc(item.ended_at))
        for item in getattr(episode, "audio_ranges", [])
        if getattr(item, "started_at", None) is not None
        and getattr(item, "ended_at", None) is not None
        and _utc(item.ended_at) > _utc(item.started_at)
    ]
    return ranges or [(_utc(episode.started_at), _utc(episode.ended_at))]


def _interval_union_seconds(intervals: Iterable[tuple[datetime, datetime]]) -> int:
    intervals = sorted(intervals)
    if not intervals:
        return 0
    total = timedelta(0)
    start, end = intervals[0]
    for next_start, next_end in intervals[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return int((total + end - start).total_seconds())


def _review_group(
    episodes: list[Any], index: int, semantic_group: Any | None = None
) -> dict[str, Any]:
    started_at = min(_utc(item.started_at) for item in episodes)
    ended_at = max(_utc(item.ended_at) for item in episodes)
    lanes = [_lane(item) for item in episodes]
    lane = _dominant(lanes, "foreground")
    reasons = sorted(
        {reason for item in episodes for reason in _episode_attention(item)}
    )
    digest = hashlib.sha1(  # noqa: S324 - stable display identity, not security
        "\0".join(item.episode_key for item in episodes).encode("utf-8")
    ).hexdigest()[:12]
    entities = [entity for item in episodes for entity in item.entities]
    span_seconds = int((ended_at - started_at).total_seconds())
    intervals = [
        (item, started_at, ended_at)
        for item in episodes
        for started_at, ended_at in _episode_intervals(item)
    ]
    captured_seconds = _interval_union_seconds(
        (started_at, ended_at) for _, started_at, ended_at in intervals
    )
    return {
        "group_id": (
            semantic_group.group_id
            if semantic_group is not None
            else f"{PROJECTION_VERSION}:{index}:{digest}"
        ),
        "started_at": started_at,
        "ended_at": ended_at,
        "title": (
            semantic_group.title
            if semantic_group is not None
            else _group_title(episodes, lane)
        ),
        "summary": semantic_group.summary if semantic_group is not None else "",
        "semantic": semantic_group is not None,
        "lane": lane,
        "episode_ids": [item.episode_id for item in episodes],
        "episode_count": len(episodes),
        "conversational_count": sum(item.conversational for item in episodes),
        "confirmed_count": sum(item.confirmed_at is not None for item in episodes),
        "duration_seconds": captured_seconds,
        "span_seconds": span_seconds,
        "gap_seconds": max(0, span_seconds - captured_seconds),
        "intervals": [
            {
                "episode_id": item.episode_id,
                "started_at": interval_start,
                "ended_at": interval_end,
            }
            for item, interval_start, interval_end in intervals
        ],
        "entities": [value for value, _ in Counter(entities).most_common(5)],
        "salience": _dominant((item.salience for item in episodes), "routine"),
        "attention_reasons": reasons,
        "needs_attention": bool(reasons),
    }


def build_day_review_projection(
    episodes: Iterable[Any],
    *,
    semantic_groups: Iterable[Any] = (),
    local_date,
    timezone_name: str,
) -> dict[str, Any]:
    """Build chronological session groups behind one compact interface."""

    ordered = sorted(
        episodes, key=lambda item: (_utc(item.started_at), _utc(item.ended_at))
    )
    episode_map = {item.episode_id: item for item in ordered}
    accepted: list[tuple[Any, list[Any]]] = []
    grouped_ids: set[str] = set()
    for group in semantic_groups:
        members = [
            episode_map[item] for item in group.episode_ids if item in episode_map
        ]
        if len(members) < 2 or len(members) != len(group.episode_ids):
            continue
        if grouped_ids & set(group.episode_ids):
            continue
        members.sort(key=lambda item: (_utc(item.started_at), _utc(item.ended_at)))
        accepted.append((group, members))
        grouped_ids.update(group.episode_ids)

    # Cluster each visual lane independently. Background capture often overlaps a
    # foreground activity; mixing them into one sequential pass creates alternating
    # one-item groups or hides the background relationship entirely.
    sessions: list[list[Any]] = []
    for lane in ("conversation", "foreground", "background"):
        lane_sessions: list[list[Any]] = []
        for episode in (
            item
            for item in ordered
            if item.episode_id not in grouped_ids and _lane(item) == lane
        ):
            if not lane_sessions:
                lane_sessions.append([episode])
                continue
            current = lane_sessions[-1]
            current_start = min(_utc(item.started_at) for item in current)
            current_end = max(_utc(item.ended_at) for item in current)
            too_far = _utc(episode.started_at) - current_end > SESSION_GAP
            too_long = _utc(episode.ended_at) - current_start > MAX_SESSION_SPAN
            if too_far or too_long:
                lane_sessions.append([episode])
            else:
                current.append(episode)
        sessions.extend(lane_sessions)

    projected = [
        _review_group(members, index, semantic_group=group)
        for index, (group, members) in enumerate(accepted)
    ]
    projected.extend(
        _review_group(session, len(projected) + index)
        for index, session in enumerate(sessions)
    )
    groups = sorted(projected, key=lambda group: group["started_at"])
    zone = ZoneInfo(timezone_name)
    day_start = datetime.combine(local_date, datetime.min.time(), tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    return {
        "version": PROJECTION_VERSION,
        "day_started_at": day_start.astimezone(timezone.utc),
        "day_ended_at": day_end.astimezone(timezone.utc),
        "episode_count": len(ordered),
        "group_count": len(groups),
        "needs_attention_count": sum(group["needs_attention"] for group in groups),
        "confirmed_count": sum(item.confirmed_at is not None for item in ordered),
        "groups": groups,
    }
