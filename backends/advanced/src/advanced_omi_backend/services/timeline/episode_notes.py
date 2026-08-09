"""Deterministic record notes for the bounds a day's analysis decided.

``Conversations/<conversation_id>.md`` records a *container*, and a container is an
artifact of how capture was chunked — a 45-minute standup with no meeting signal is
already two of them. The episode is the decided bound, so for capture evidence the
durable record follows the episode instead.

These notes are written deterministically, before the day's write agent runs, for the
same reason the conversation path keeps a source-preserving fallback: the record of
*what happened and what was said* must not depend on a model completing. The agent's
job is the day note and the durable People/Topic edits on top of them.

Only episodes worth a standalone record get one — everything the day held is still in
``Daily/<date>.md``. An episode note is also the only place a long transcript survives
in full, since the day digest trims transcripts to fit its budget.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from advanced_omi_backend.models.timeline import TimelineEpisode

logger = logging.getLogger(__name__)

EPISODE_FOLDER = "Episodes"
# Recorded on its own when people actually spoke, or when the day's analysis judged the
# stretch to stand out. Routine and background episodes stay in the day note only.
RECORDED_SALIENCE = frozenset({"notable", "highlight"})
_UNSAFE_TITLE = re.compile(r"[\\/:*?\"<>|#^\[\]]+")


def _as_utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; they are UTC, not node-local."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _slug(title: str) -> str:
    """A note title that is safe on Windows, Syncthing, and Obsidian wikilinks."""

    cleaned = _UNSAFE_TITLE.sub(" ", title or "").replace("\n", " ")
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:80].strip() or "Untitled episode"


def should_record(episode: TimelineEpisode) -> bool:
    return bool(episode.conversational) or episode.salience in RECORDED_SALIENCE


def episode_note_path(episode: TimelineEpisode, timezone_name: str) -> str:
    """Vault-relative path for this episode's record note.

    The local date and start time lead the title so a folder listing reads as a
    timeline, and so two episodes that a day named identically cannot collide.
    """

    local = _as_utc(episode.started_at).astimezone(ZoneInfo(timezone_name))
    return (
        f"{EPISODE_FOLDER}/{local.strftime('%Y-%m-%d %H%M')} "
        f"{_slug(episode.title)}.md"
    )


def render_episode_note(
    episode: TimelineEpisode,
    timezone_name: str,
    transcripts: dict[str, str],
    day_note_name: str,
) -> str:
    zone = ZoneInfo(timezone_name)
    started = _as_utc(episode.started_at).astimezone(zone)
    ended = _as_utc(episode.ended_at).astimezone(zone)
    minutes = max(0.0, (ended - started).total_seconds() / 60)
    recordings = sorted(
        {str(item) for item in episode.related_conversation_ids if item}
        | {
            str(ref.metadata["conversation_id"])
            for ref in episode.evidence_refs
            if ref.metadata.get("conversation_id")
        }
    )

    lines = [
        "---",
        "categories:",
        f'  - "[[{EPISODE_FOLDER}]]"',
        f"episode_id: {json.dumps(episode.episode_id)}",
        f"date: {json.dumps(started.date().isoformat())}",
        f"started_at: {json.dumps(started.isoformat())}",
        f"ended_at: {json.dumps(ended.isoformat())}",
        f"duration_minutes: {minutes:g}",
        f"kind: {json.dumps(str(episode.kind))}",
        f"salience: {json.dumps(str(episode.salience))}",
        f"conversational: {json.dumps(bool(episode.conversational))}",
        "recordings:",
        *(f"  - {json.dumps(item)}" for item in recordings),
        "people: []",
        "topics: []",
        "---",
        f"## {_slug(episode.title)}",
        "",
        f"Part of [[{day_note_name}]] · {started:%H:%M}–{ended:%H:%M} "
        f"({timezone_name}).",
        "",
        "### Summary",
        (episode.summary or "").strip() or "No summary was produced for this episode.",
        "",
    ]
    if episode.entities:
        lines += ["### Entities", ", ".join(episode.entities), ""]
    if episode.assertions:
        lines.append("### Assertions")
        # role and confidence are kept verbatim: they are what separates something the
        # user said from media dialogue or application output, and a record note that
        # drops them invites a later reader to promote the wrong thing into a fact.
        lines += [
            f"- [{assertion.role} · confidence {assertion.confidence:.2f}] "
            f"{assertion.claim}"
            for assertion in episode.assertions
        ]
        lines.append("")

    cited = [transcripts[item] for item in recordings if transcripts.get(item)]
    if cited:
        lines.append("### Transcript")
        lines.append("")
        lines.extend(cited)
        lines.append("")
    return "\n".join(lines)


def write_episode_notes(
    user_root: Path,
    episodes: Iterable[TimelineEpisode],
    timezone_name: str,
    transcripts: dict[str, str],
    day_note_name: str,
) -> list[str]:
    """Write a record note per recordable episode. Returns the paths written.

    A failure here is logged and skipped rather than raised: these notes are an
    addition to the day write, and losing one must not cost the whole day its memory.
    """

    written: list[str] = []
    for episode in episodes:
        if not should_record(episode):
            continue
        relative = episode_note_path(episode, timezone_name)
        path = user_root / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                render_episode_note(episode, timezone_name, transcripts, day_note_name),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("Episode note %s could not be written: %s", relative, error)
            continue
        written.append(relative)
    return written
