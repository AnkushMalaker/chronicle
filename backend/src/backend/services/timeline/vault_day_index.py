"""Deterministic Daily-note index rendering used only by reviewed vault writes."""

from __future__ import annotations

import re
from pathlib import Path

_DAY_DIGEST_EPISODE_HEADING_RE = re.compile(
    r"^###\s+(\d{2}:\d{2}–\d{2}:\d{2})\s+·\s+(.+?)\s+·\s+(.+?)\s*$",
    re.MULTILINE,
)
_H2_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _line_value(block: str, key: str) -> str:
    prefix = f"{key}:"
    for line in block.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def render_day_episode_index(day_digest: str) -> str:
    """Render the trusted Daily ``## Episodes`` index from a reviewed digest."""

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
        episode_key = _line_value(block, "episode_key")
        conversation_ids = list(
            dict.fromkeys(
                item.strip()
                for item in _line_value(block, "conversation_ids").split(",")
                if item.strip()
            )
        )
        text = f"- {clock} · {kind} · {salience}"
        label = title or summary
        if label:
            text += f" — {label}"
        if conversation_ids:
            sources = ", ".join(
                f"[[Conversations/{conversation_id}|source]]"
                for conversation_id in conversation_ids
            )
            text += f" · sources: {sources}"
        if episode_key:
            text += f" <!-- episode_key:{episode_key} -->"
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
    """Install a reviewed digest into one Daily note, returning whether it changed."""

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
