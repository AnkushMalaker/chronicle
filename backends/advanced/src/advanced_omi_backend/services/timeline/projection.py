"""Deterministic day projections rendered from active timeline episodes.

The day view is a projection, not a second interpretation pass. Whenever a material
episode revision is published, this module recomputes the desired ``## Episodes``
index for every local day the episode touches and writes it only when the content
actually differs — a reconciliation that changed nothing writes nothing.

Two invariants shape the queries here:

- **Cross-midnight.** An episode belongs to every local day its absolute interval
  intersects, so days are selected by UTC-interval overlap against
  :func:`evidence.day_bounds`, never by ``local_date`` equality.
- **Pipeline scoping.** The day-scoped analysis pipeline and rolling reconciliation
  both write ``TimelineEpisode`` rows during the transition. A projection renders only
  the rows belonging to the user's active pipeline, so the two writers can never
  render into the same note.

The vault-note helpers below were lifted verbatim from the Chronicle memory provider,
which now delegates to them, so the settled-day write and the incremental projection
install byte-identical indexes.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from advanced_omi_backend.models.timeline import TimelineDay, TimelineEpisode
from advanced_omi_backend.models.user import User

from .evidence import day_bounds
from .timezone import canonical_timezone

logger = logging.getLogger(__name__)

_DAY_DIGEST_EPISODE_HEADING_RE = re.compile(
    r"^###\s+(\d{2}:\d{2}–\d{2}:\d{2})\s+·\s+(.+?)\s+·\s+(.+?)\s*$",
    re.MULTILINE,
)
_H2_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Rows a projection must never render: superseded revisions are history.
ACTIVE_EPISODE_QUERY = {"status": {"$ne": "superseded"}}


def _line_value(block: str, key: str) -> str:
    prefix = f"{key}:"
    for line in block.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def render_day_episode_index(day_digest: str) -> str:
    """Render the trusted Daily ``## Episodes`` index from a day digest.

    The memory agent is useful for semantic People/Topic deltas, but this section is a
    mechanical mirror of the active timeline run. Keeping it deterministic prevents a
    good day write from failing because the model omitted or partially rewrote episode
    ranges. The detailed summary already lives on ``TimelineEpisode`` and is supplied
    to the agent for deciding durable facts; copying it into the Daily index duplicates
    the same information and makes busy days needlessly huge. The vault therefore keeps
    only the concise episode title (falling back to the summary when a title is absent).
    """

    matches = list(_DAY_DIGEST_EPISODE_HEADING_RE.finditer(day_digest or ""))
    bullets: list[str] = []
    for index, match in enumerate(matches):
        block_start = match.end()
        block_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(day_digest)
        )
        block = day_digest[block_start:block_end]
        clock, kind, salience = (part.strip() for part in match.groups())
        title = _line_value(block, "title")
        summary = _line_value(block, "summary")
        text = f"- {clock} · {kind} · {salience}"
        label = title or summary
        if label:
            text += f" — {label}"
        bullets.append(text)
    return "\n".join(bullets)


def replace_h2_section(note: str, heading: str, body: str) -> str:
    section = f"## {heading}\n\n{body.rstrip()}\n"
    match = next(
        (
            candidate
            for candidate in _H2_SECTION_RE.finditer(note)
            if candidate.group(1).strip().lower() == heading.lower()
        ),
        None,
    )
    if match is None:
        prefix = note.rstrip()
        return f"{prefix}\n\n{section}" if prefix else section

    next_heading = _H2_SECTION_RE.search(note, match.end())
    section_end = next_heading.start() if next_heading else len(note)
    before = note[: match.start()].rstrip()
    after = note[section_end:].lstrip("\n")
    pieces = []
    if before:
        pieces.append(before)
    pieces.append(section.rstrip())
    if after:
        pieces.append(after.rstrip())
    return "\n\n".join(pieces) + "\n"


def ensure_day_episode_index(note_path: Path, local_date: str, day_digest: str) -> bool:
    body = render_day_episode_index(day_digest)
    if not body:
        return False
    try:
        note = note_path.read_text(encoding="utf-8")
    except OSError:
        note = f"# {local_date}\n"
    updated = replace_h2_section(note, "Episodes", body)
    if updated == note:
        return False
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(updated, encoding="utf-8")
    return True


def _utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; compare everything in UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def affected_local_dates(
    started_at: datetime, ended_at: datetime, timezone_name: str
) -> list[date]:
    """Every local date the absolute interval ``[started_at, ended_at)`` intersects.

    An episode running past midnight belongs to both days, so a single ``local_date``
    field cannot answer this. Candidate dates are enumerated from the local calendar
    and then tested against :func:`day_bounds`, which is what makes this correct on a
    DST transition: a local day there is 23 or 25 hours long, and the offset used to
    derive the calendar date is not the offset that bounds it.
    """

    timezone_name = canonical_timezone(timezone_name)
    zone = ZoneInfo(timezone_name)
    start, end = _utc(started_at), _utc(ended_at)
    if end < start:
        start, end = end, start

    first = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    dates: list[date] = []
    # One day of slack on either side: a UTC instant can fall in a neighbouring local
    # date under an offset the naive conversion above did not use.
    candidate = first - timedelta(days=1)
    while candidate <= last + timedelta(days=1):
        day_start, day_end = day_bounds(candidate, timezone_name)
        # Half-open on both sides, except for a zero-length interval, which belongs to
        # the day containing its instant.
        if day_start <= start < day_end or (start < day_end and end > day_start):
            dates.append(candidate)
        candidate += timedelta(days=1)
    return dates


async def _active_pipeline(user_id: str) -> str:
    user = await User.get(user_id)
    if user is None:
        return "day"
    return getattr(user, "active_timeline_pipeline", "day") or "day"


async def active_day_episodes(
    user_id: str,
    local_date: date,
    timezone_name: str,
    *,
    pipeline: Optional[str] = None,
) -> list[TimelineEpisode]:
    """Active episodes of the user's pipeline intersecting this local day."""

    timezone_name = canonical_timezone(timezone_name)
    day_start, day_end = day_bounds(local_date, timezone_name)
    if pipeline is None:
        pipeline = await _active_pipeline(user_id)

    query: dict = {
        "user_id": user_id,
        "pipeline": pipeline,
        "started_at": {"$lt": day_end},
        "ended_at": {"$gt": day_start},
        **ACTIVE_EPISODE_QUERY,
    }

    if pipeline == "day":
        # Day-pipeline rows are per-run: only the published generation is readable,
        # exactly as the day read route resolves them.
        day = await TimelineDay.find_one(
            TimelineDay.user_id == user_id,
            TimelineDay.local_date == local_date,
            TimelineDay.timezone == timezone_name,
        )
        if day is None or not day.active_run_id:
            return []
        query["run_id"] = day.active_run_id

    episodes = await TimelineEpisode.find(query).to_list()
    episodes.sort(key=lambda item: (_utc(item.started_at), _utc(item.ended_at)))
    return episodes


async def refresh_day_projection(
    user_id: str, local_date: date, timezone_name: str
) -> bool:
    """Rewrite this local day's ``## Episodes`` index. True iff content changed."""

    # Lazy: the memory package imports this module's caller (the Chronicle provider
    # delegates its note helpers here), so importing it at module scope is circular.
    from advanced_omi_backend.services.memory.agent.memory_agent import day_note_path
    from advanced_omi_backend.services.memory.vault_manager import ConvDocVaultManager

    from .memory import build_day_index_digest

    timezone_name = canonical_timezone(timezone_name)
    pipeline = await _active_pipeline(user_id)
    episodes = await active_day_episodes(
        user_id, local_date, timezone_name, pipeline=pipeline
    )

    if pipeline == "rolling":
        # Rolling users get their day row created by the projection: there is no
        # day-scoped analysis run to create it. ``active_run_id`` belongs to the day
        # pipeline and is deliberately left alone.
        await _ensure_day_row(user_id, local_date, timezone_name)

    digest = build_day_index_digest(episodes, local_date, timezone_name)
    note_path = ConvDocVaultManager().user_root(user_id) / day_note_path(
        local_date.isoformat()
    )
    changed = ensure_day_episode_index(note_path, local_date.isoformat(), digest)
    if changed:
        logger.info(
            "Day projection updated for %s %s (%d episode(s), pipeline=%s)",
            user_id,
            local_date,
            len(episodes),
            pipeline,
        )
    return changed


async def _ensure_day_row(user_id: str, local_date: date, timezone_name: str) -> None:
    existing = await TimelineDay.find_one(
        TimelineDay.user_id == user_id,
        TimelineDay.local_date == local_date,
        TimelineDay.timezone == timezone_name,
    )
    if existing is not None:
        return
    await TimelineDay(
        user_id=user_id, local_date=local_date, timezone=timezone_name
    ).insert()


async def refresh_projections(
    user_id: str,
    dates: Optional[list[date]] = None,
    *,
    episode: Optional[TimelineEpisode] = None,
    timezone_name: Optional[str] = None,
) -> list[date]:
    """Post-publish hook: refresh the days an episode revision affects.

    Returns the dates whose projection content actually changed, so a caller can
    report real work rather than the number of days it looked at.
    """

    if timezone_name is None and episode is not None:
        timezone_name = episode.timezone
    if timezone_name is None:
        raise ValueError("refresh_projections needs a timezone or an episode")
    timezone_name = canonical_timezone(timezone_name)

    if dates is None:
        if episode is None:
            raise ValueError("refresh_projections needs dates or an episode")
        dates = affected_local_dates(
            episode.started_at, episode.ended_at, timezone_name
        )

    changed: list[date] = []
    for local_date in sorted(set(dates)):
        if await refresh_day_projection(user_id, local_date, timezone_name):
            changed.append(local_date)
    return changed
