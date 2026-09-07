"""Pure rendering helpers for snapshot-fenced Timeline memory review."""

import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.models.timeline import TimelineEpisode, TimelineSemanticGroupRevision

from . import activity_policy
from .executor import settings_dict
from .recording_refs import episode_conversation_ids
from .timezone import canonical_timezone

# ~15K tokens, the size audited memory prompts already reach successfully. Larger day
# bundles are trimmed rather than truncated silently.
_DEFAULT_MAX_DIGEST_CHARS = 60000

_SALIENCE_RANK = {"background": 0, "routine": 1, "notable": 2, "highlight": 3}

# A day in one of these states is done with; the settled-day scan must not pick it up
# again. Named once because a scan that forgets a member silently re-runs the agent
# over days it already settled.
_TERMINAL_MEMORY_STATES = ("written", "partial", "skipped", "no_changes")


def memory_settings() -> dict[str, Any]:
    block = settings_dict().get("memory") or {}
    return block if isinstance(block, dict) else {}


def _setting(name: str, default: int) -> int:
    try:
        return int(memory_settings().get(name, default))
    except (TypeError, ValueError):
        return default


def _as_utc(value: datetime) -> datetime:
    """Mongo returns naive datetimes; they are UTC, not node-local.

    Without this an episode's clock is shifted by the host's offset, and comparing a
    stored timestamp against ``utcnow()`` raises outright.
    """

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _clock(episode: TimelineEpisode, zone: ZoneInfo) -> str:
    started = _as_utc(episode.started_at).astimezone(zone).strftime("%H:%M")
    ended = _as_utc(episode.ended_at).astimezone(zone).strftime("%H:%M")
    return f"{started}–{ended}"


def _is_media_kind(kind: str) -> bool:
    """Whether a human/agent type names observed media rather than lived activity."""

    return "media" in re.split(r"[^a-z0-9]+", (kind or "").lower())


def episode_semantic_memory_enabled(episode: TimelineEpisode) -> bool:
    """Apply the person's episode-level vault policy.

    Media is visible Chronicle evidence, but it is not a durable fact about the user.
    It reaches the semantic memory agent only after an explicit ``remember`` opt-in.
    """

    if activity_policy.episode_is_recording_only(episode):
        return False
    policy = episode.memory_policy
    if policy == "remember":
        return True
    if policy == "reference":
        return False
    return not _is_media_kind(episode.kind)


_MEDIA_ACTIVITY_PREFIX = re.compile(
    r"^(?:watching|viewing|listening\s+to|playing|discussing|showing|media(?:\s+(?:about|with))?)\s+",
    re.IGNORECASE,
)


def _media_reference_title(title: str) -> str:
    """Turn an agent's activity-like media title into a neutral content reference."""

    subject = _MEDIA_ACTIVITY_PREFIX.sub("", title.strip()).strip(" .:-")
    if not subject:
        return "Media"
    if subject[0].isupper() and not subject[:2].isupper():
        subject = subject[0].lower() + subject[1:]
    return f"Media: {subject}"


def render_episode(
    episode: TimelineEpisode,
    zone: ZoneInfo,
) -> str:
    """Render one episode for the write agent.

    Assertions keep their ``role`` and ``confidence`` because those are what stop media
    dialogue, application output, and assistant text from being recorded as facts about
    the user. Raw evidence is deliberately excluded: the vault receives the timeline's
    bounded interpretation, while transcripts remain in MongoDB as source evidence.
    """

    lines = [
        f"### {_clock(episode, zone)} · {episode.kind} · {episode.salience}",
        f"title: {episode.title}",
        f"episode_key: {episode.episode_key}",
        f"source_started_at: {_as_utc(episode.started_at).astimezone(zone).isoformat()}",
        f"source_ended_at: {_as_utc(episode.ended_at).astimezone(zone).isoformat()}",
        f"source_timezone: {zone.key}",
    ]
    conversation_ids = episode_conversation_ids(episode)
    if conversation_ids:
        lines.append(f"conversation_ids: {', '.join(conversation_ids)}")
    for evidence in episode.evidence_refs:
        lines.append(
            f"evidence_time: {evidence.evidence_id} · {_as_utc(evidence.started_at).astimezone(zone).isoformat()}"
            + (
                f" to {_as_utc(evidence.ended_at).astimezone(zone).isoformat()}"
                if evidence.ended_at
                else ""
            )
        )
    if episode.summary:
        lines.append(f"summary: {episode.summary}")
    if episode.entities:
        lines.append(f"entities: {', '.join(episode.entities)}")
    if episode.attributes:
        rendered = "; ".join(
            f"{key}={value}" for key, value in sorted(episode.attributes.items())
        )
        lines.append(f"attributes: {rendered}")
    if episode.assertions:
        lines.append("assertions:")
        lines.extend(
            f"- [{assertion.role} · confidence {assertion.confidence:.2f}] "
            f"{assertion.claim}"
            for assertion in episode.assertions
        )
    return "\n".join(lines)


def build_day_digest(
    episodes: list[TimelineEpisode],
    local_date: date,
    timezone_name: str,
    max_chars: int | None = None,
    semantic_group_revisions: list[TimelineSemanticGroupRevision] | None = None,
) -> tuple[str, list[str]]:
    """Render a day of episodes within a character budget.

    Episodes are shed lowest-salience-first when summaries overflow. Conversational
    episodes are never shed. Raw evidence is never part of this digest: it remains in
    the corpus and timeline records instead of being copied into the memory vault.
    """

    budget = (
        max_chars
        if max_chars is not None
        else _setting("max_digest_chars", _DEFAULT_MAX_DIGEST_CHARS)
    )
    zone = ZoneInfo(timezone_name)
    ordered = sorted(
        (episode for episode in episodes if episode_semantic_memory_enabled(episode)),
        key=lambda item: (item.started_at, item.ended_at),
    )
    if not ordered:
        return "", []
    active_groups = [
        group
        for group in (semantic_group_revisions or [])
        if {item.episode_id for item in ordered}.issuperset(group.episode_ids)
    ]
    if active_groups:
        return _build_grouped_day_digest(
            ordered,
            active_groups,
            local_date,
            timezone_name,
            budget,
        )
    rendered = {
        episode.episode_id: render_episode(episode, zone) for episode in ordered
    }

    header = (
        f"Local day {local_date.isoformat()} ({timezone_name}), "
        f"{len(ordered)} episode(s)."
    )
    keep = {episode.episode_id for episode in ordered}

    def total() -> int:
        return len(header) + sum(len(rendered[episode_id]) + 2 for episode_id in keep)

    dropped: list[str] = []

    # Shed only low-salience non-conversational summaries. Conversations remain
    # represented by their bounded episode summary, never by copied dialogue.
    droppable = sorted(
        (episode for episode in ordered if not episode.conversational),
        key=lambda item: (
            _SALIENCE_RANK.get(item.salience, 1),
            (item.ended_at - item.started_at),
        ),
    )
    for episode in droppable:
        if total() <= budget:
            break
        keep.discard(episode.episode_id)
        dropped.append(episode.title)

    # A header claiming 13 episodes above a body holding 4 is a digest that lies to the
    # model, and both a 27B local model and DeepSeek V4 Pro duly reported having covered
    # "all four episodes" of that thirteen-episode day. Say what is actually here.
    if len(keep) != len(ordered):
        header = (
            f"Local day {local_date.isoformat()} ({timezone_name}), "
            f"{len(keep)} of {len(ordered)} episode(s) — "
            f"{len(ordered) - len(keep)} omitted to fit; the day was longer than this."
        )

    body = "\n\n".join(
        rendered[episode.episode_id]
        for episode in ordered
        if episode.episode_id in keep
    )
    return f"{header}\n\n{body}", dropped


def _build_grouped_day_digest(
    episodes: list[TimelineEpisode],
    groups: list[TimelineSemanticGroupRevision],
    local_date: date,
    timezone_name: str,
    budget: int,
) -> tuple[str, list[str]]:
    """Render accepted semantic groups once while retaining member provenance."""

    zone = ZoneInfo(timezone_name)
    episode_map = {episode.episode_id: episode for episode in episodes}
    grouped_ids = {episode_id for group in groups for episode_id in group.episode_ids}
    items: list[dict[str, Any]] = []
    for group in groups:
        members = sorted(
            (episode_map[episode_id] for episode_id in group.episode_ids),
            key=lambda item: (item.started_at, item.ended_at),
        )
        member_lines = "\n".join(
            f"- {_clock(member, zone)} · {member.title} · episode_key: {member.episode_key}"
            for member in members
        )
        rendered = (
            f"### Semantic group · {group.title}\n"
            f"span: {_clock(members[0], zone).split('–', 1)[0]}–"
            f"{_clock(members[-1], zone).split('–', 1)[-1]}\n"
            f"summary: {group.summary}\n"
            f"member episodes:\n{member_lines}"
        )
        items.append(
            {
                "id": f"group:{group.group_key}",
                "title": group.title,
                "rendered": rendered,
                "conversational": any(member.conversational for member in members),
                "salience": max(
                    members,
                    key=lambda member: _SALIENCE_RANK.get(member.salience, 1),
                ).salience,
                "duration": sum(
                    (member.ended_at - member.started_at).total_seconds()
                    for member in members
                ),
                "started_at": members[0].started_at,
            }
        )
    for episode in episodes:
        if episode.episode_id in grouped_ids:
            continue
        items.append(
            {
                "id": episode.episode_id,
                "title": episode.title,
                "rendered": render_episode(episode, zone),
                "conversational": episode.conversational,
                "salience": episode.salience,
                "duration": (episode.ended_at - episode.started_at).total_seconds(),
                "started_at": episode.started_at,
            }
        )
    items.sort(key=lambda item: item["started_at"])
    header = (
        f"Local day {local_date.isoformat()} ({timezone_name}), "
        f"{len(items)} reviewed semantic item(s) from {len(episodes)} episode(s)."
    )
    keep = {item["id"] for item in items}

    def total() -> int:
        return len(header) + sum(
            len(item["rendered"]) + 2 for item in items if item["id"] in keep
        )

    dropped: list[str] = []
    for item in sorted(
        (item for item in items if not item["conversational"]),
        key=lambda item: (
            _SALIENCE_RANK.get(item["salience"], 1),
            item["duration"],
        ),
    ):
        if total() <= budget:
            break
        keep.discard(item["id"])
        dropped.append(item["title"])
    if len(keep) != len(items):
        header = (
            f"Local day {local_date.isoformat()} ({timezone_name}), "
            f"{len(keep)} of {len(items)} reviewed semantic item(s) — "
            f"{len(items) - len(keep)} omitted to fit."
        )
    body = "\n\n".join(item["rendered"] for item in items if item["id"] in keep)
    return f"{header}\n\n{body}", dropped


def build_day_index_digest(
    episodes: list[TimelineEpisode], local_date: date, timezone_name: str
) -> str:
    """Render every active episode for Chronicle's concise Daily index.

    This source is deliberately separate from the bounded semantic digest: shedding a
    low-salience summary to fit the model prompt must never remove that episode's exact
    range from the deterministic index.
    """

    zone = ZoneInfo(timezone_name)
    ordered = sorted(episodes, key=lambda item: (item.started_at, item.ended_at))
    body = "\n\n".join(
        f"### {_clock(episode, zone)} · {episode.kind} · {episode.salience}\n"
        f"title: {_media_reference_title(episode.title) if _is_media_kind(episode.kind) else episode.title}\n"
        f"episode_key: {episode.episode_key}"
        + (
            "\nconversation_ids: " + ", ".join(episode_conversation_ids(episode))
            if episode_conversation_ids(episode)
            else ""
        )
        for episode in ordered
    )
    return (
        f"Local day {local_date.isoformat()} ({timezone_name}), "
        f"{len(ordered)} episode(s).\n\n{body}"
    )
