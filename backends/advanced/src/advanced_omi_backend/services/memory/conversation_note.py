"""Validation and deterministic rendering for generated conversation notes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ruamel.yaml import YAML

_YAML = YAML(typ="safe")
_FRONTMATTER_BOUNDARY = re.compile(r"^---\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H3 = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_PLACEHOLDERS = {"", "-", "none", "n/a", "unknown", "untitled", "[ ]", "- [ ]"}


class ConversationNoteError(ValueError):
    """The model output cannot be made into a substantive conversation note."""


def _frontmatter_and_body(content: str) -> tuple[dict[str, Any], str]:
    boundaries = list(_FRONTMATTER_BOUNDARY.finditer(content))
    if len(boundaries) < 2:
        raise ConversationNoteError("missing YAML frontmatter")
    first, second = boundaries[:2]
    try:
        metadata = _YAML.load(content[first.end() : second.start()]) or {}
    except Exception as exc:
        raise ConversationNoteError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ConversationNoteError("frontmatter must be a mapping")
    body = f"{content[: first.start()]}\n{content[second.end() :]}".strip()
    return metadata, body


def _sections(body: str) -> dict[str, str]:
    matches = list(_H3.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip().casefold()] = body[match.end() : end].strip()
    return sections


def _substantive_lines(value: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in value.splitlines():
        cleaned = line.strip()
        payload = re.sub(r"^-\s*(?:\[[ xX]\]\s*)?", "", cleaned).strip()
        if payload.casefold() in _PLACEHOLDERS:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _list_property(metadata: dict[str, Any], name: str) -> list[str]:
    value = metadata.get(name, [])
    if not isinstance(value, list):
        return []
    items = list(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )
    if name == "people":
        items = [
            item
            for item in items
            if not re.search(r"\bunknown speaker(?:\s+\d+)?\b", item, re.IGNORECASE)
        ]
    return items


def _render_list(name: str, values: Iterable[str]) -> list[str]:
    values = list(values)
    if not values:
        return [f"{name}: []"]
    return [f"{name}:", *(f"  - {json.dumps(value)}" for value in values)]


def canonicalize_conversation_note(
    path: Path,
    *,
    conversation_id: str,
    date: str,
    duration_minutes: float | None,
    title: str | None,
) -> None:
    """Validate model-written content and replace its metadata with trusted values.

    The LLM still extracts the semantic content, but it never controls identity,
    chronology, or duration. Exact repeated lines are removed while rendering.
    """
    content = path.read_text(encoding="utf-8")
    if "```" in content or "\\n" in content:
        raise ConversationNoteError("note contains a code fence or escaped newlines")
    metadata, body = _frontmatter_and_body(content)
    sections = _sections(body)

    summary_lines = _substantive_lines(sections.get("summary", ""))
    fact_lines = _substantive_lines(sections.get("key facts", ""))
    action_lines = _substantive_lines(sections.get("action items", ""))
    if len(" ".join(summary_lines)) < 20:
        raise ConversationNoteError("summary is empty or placeholder content")
    if not fact_lines:
        raise ConversationNoteError("key facts are empty or placeholder content")

    heading = _H2.search(body)
    generated_title = heading.group(1).strip() if heading else ""
    if generated_title.casefold() in _PLACEHOLDERS:
        generated_title = (title or "").strip()
    if generated_title.casefold() in _PLACEHOLDERS:
        raise ConversationNoteError("title is empty or placeholder content")

    duration = "" if duration_minutes is None else f"{float(duration_minutes):g}"
    people = _list_property(metadata, "people")
    topics = _list_property(metadata, "topics")
    hermes_links = [item for item in people if item.casefold() == "[[hermes]]"]
    people = [item for item in people if item.casefold() != "[[hermes]]"]
    if hermes_links and not any(item.casefold() == "[[hermes]]" for item in topics):
        topics.append("[[Hermes]]")

    lines = [
        "---",
        "categories:",
        '  - "[[Conversations]]"',
        f"conversation_id: {json.dumps(str(conversation_id))}",
        f"date: {json.dumps(str(date))}",
        *_render_list("people", people),
        *_render_list("topics", topics),
        f"duration_minutes: {duration}",
        "---",
        f"## {generated_title}",
        "",
        "### Summary",
        *summary_lines,
        "",
        "### Key Facts",
        *fact_lines,
        "",
        "### Action Items",
        *(action_lines or ["- [ ]"]),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_source_fallback_conversation_note(
    path: Path,
    *,
    transcript: str,
    conversation_id: str,
    date: str,
    duration_minutes: float | None,
    title: str | None,
) -> None:
    """Write a minimal lossless note after both semantic LLM attempts fail."""
    excerpt = " ".join(transcript.split())[:500].strip()
    if not excerpt:
        raise ConversationNoteError(
            "cannot create source fallback for empty transcript"
        )
    safe_excerpt = excerpt.replace('"', "'")
    fallback_title = (title or "").strip() or f"Conversation on {date[:10]}"
    duration = "" if duration_minutes is None else f"{float(duration_minutes):g}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                "categories:",
                '  - "[[Conversations]]"',
                f"conversation_id: {json.dumps(str(conversation_id))}",
                f"date: {json.dumps(str(date))}",
                "people: []",
                "topics: []",
                f"duration_minutes: {duration}",
                "---",
                f"## {fallback_title}",
                "",
                "### Summary",
                f'The source transcript contains this short utterance: "{safe_excerpt}"',
                "",
                "### Key Facts",
                f'- Verbatim source excerpt: "{safe_excerpt}"',
                "",
                "### Action Items",
                "- [ ]",
                "",
            ]
        ),
        encoding="utf-8",
    )
