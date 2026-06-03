"""Utility functions for FalkorDB-based conversation memory.

Pure functions for parsing conversation documents (markdown), computing
hybrid search scores, and related helpers. No I/O or external dependencies
beyond the standard library.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Texts that indicate an empty/placeholder section
_EMPTY_PATTERNS = frozenset({"none", "- none", "n/a", "- n/a", ""})


@dataclass
class Frontmatter:
    """YAML frontmatter from a conversation document."""

    conversation_id: str = ""
    date: str = ""
    speakers: str = ""
    duration: str = ""
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class Person:
    """A person mentioned in the ### People section."""

    name: str
    description: str = ""


@dataclass
class ActionItem:
    """An action item from the ### Action Items section."""

    text: str
    done: bool = False


@dataclass
class Section:
    """A ### section from a conversation document."""

    title: str  # e.g. "Summary", "Key Facts", "People", "Action Items"
    body: str  # raw text content under the header


@dataclass
class ConversationDoc:
    """Parsed conversation document with typed fields."""

    title: str  # from ## heading
    frontmatter: Frontmatter
    sections: List[Section]  # only non-empty ### sections
    people: List[Person]
    action_items: List[ActionItem]
    raw_markdown: str  # full original markdown


def parse_conversation_doc(markdown: str) -> ConversationDoc:
    """Parse a conversation document into typed structure.

    Extracts frontmatter, document title (## heading), sections (### headings),
    people, and action items. Empty/placeholder sections are dropped.
    """
    frontmatter = _parse_frontmatter(markdown)
    title = _extract_title(markdown)
    raw_sections = _split_sections(markdown)
    people = _parse_people(markdown)
    action_items = _parse_action_items(markdown)

    # Filter out empty sections
    sections = [s for s in raw_sections if _section_has_content(s.body)]

    return ConversationDoc(
        title=title,
        frontmatter=frontmatter,
        sections=sections,
        people=people,
        action_items=action_items,
        raw_markdown=markdown,
    )


def _section_has_content(text: str) -> bool:
    """Return False for placeholder text like '- None' or empty."""
    stripped = text.strip().lower()
    return stripped not in _EMPTY_PATTERNS and len(stripped) > 5


def _parse_frontmatter(markdown: str) -> Frontmatter:
    """Extract YAML frontmatter."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", markdown, re.DOTALL)
    if not fm_match:
        return Frontmatter()
    raw = {}
    for line in fm_match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            raw[key.strip()] = value.strip().strip('"').strip("'")
    return Frontmatter(
        conversation_id=raw.get("conversation_id", ""),
        date=raw.get("date", ""),
        speakers=raw.get("speakers", ""),
        duration=raw.get("duration_minutes", raw.get("duration", "")),
        raw=raw,
    )


def _extract_title(markdown: str) -> str:
    """Extract the ## heading as document title."""
    match = re.search(r"^##\s+(.+)$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _split_sections(markdown: str) -> List[Section]:
    """Split markdown into Section objects by ### headers.

    Only returns ### sections — the ## title and frontmatter are excluded.
    """
    # Strip frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", markdown, re.DOTALL)
    content = markdown[fm_match.end() :] if fm_match else markdown

    parts = re.split(r"^(###\s+.+)$", content, flags=re.MULTILINE)

    sections = []
    # Skip parts[0] — that's text before the first ### (the ## title line)
    i = 1
    while i < len(parts):
        if parts[i].startswith("###"):
            title = parts[i].replace("###", "").strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            sections.append(Section(title=title, body=body))
            i += 2
        else:
            i += 1

    return sections


def _parse_people(markdown: str) -> List[Person]:
    """Parse the ### People section."""
    match = re.search(
        r"^###\s+People\s*\n(.*?)(?=^###|\Z)", markdown, re.MULTILINE | re.DOTALL
    )
    if not match:
        return []

    people = []
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()

        paren_match = re.match(r'^["""]?(.+?)["""]?\s*\((.+?)\)\s*$', line)
        if paren_match:
            people.append(
                Person(
                    name=paren_match.group(1).strip(),
                    description=paren_match.group(2).strip(),
                )
            )
        else:
            name = line.strip('"').strip("'")
            if name and name.lower() != "none":
                people.append(Person(name=name))

    return people


def _parse_action_items(markdown: str) -> List[ActionItem]:
    """Parse the ### Action Items section."""
    match = re.search(
        r"^###\s+Action Items\s*\n(.*?)(?=^###|\Z)", markdown, re.MULTILINE | re.DOTALL
    )
    if not match:
        return []

    items = []
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()

        done = False
        if line.startswith("[x]") or line.startswith("[X]"):
            done = True
            line = line[3:].strip()
        elif line.startswith("[ ]"):
            line = line[3:].strip()

        if line and line.lower() != "none":
            items.append(ActionItem(text=line, done=done))

    return items


# ---------------------------------------------------------------------------
# Hybrid search scoring
# ---------------------------------------------------------------------------


def compute_hybrid_scores(
    vector_results: List[Dict],
    fulltext_results: List[Dict],
    bfs_results: Optional[List[Dict]] = None,
    vector_weight: float = 0.7,
    text_weight: float = 0.3,
    bfs_weight: float = 0.2,
    recency_half_life_days: float = 30.0,
    recency_floor: float = 0.5,
) -> List[Dict]:
    """Merge vector + full-text + (optional) BFS results with recency bias.

    Each result dict must have: 'chunk_id', 'score', 'date' (ISO string or datetime).
    For ``bfs_results`` the ``score`` is integer shared-entity-count; it is
    normalized by the maximum in the BFS list so it lands in [0, 1] like the
    other two sources. Chunks reachable only via BFS contribute via
    ``bfs_weight``; chunks already present from vector/BM25 get a small bonus.
    Additional fields are preserved.
    """
    now = datetime.now(timezone.utc)
    merged: Dict[str, Dict] = {}

    for r in vector_results:
        cid = r["chunk_id"]
        merged[cid] = {
            **r,
            "vector_score": r["score"],
            "text_score": 0.0,
            "bfs_score": 0.0,
        }

    for r in fulltext_results:
        cid = r["chunk_id"]
        if cid in merged:
            merged[cid]["text_score"] = r["score"]
        else:
            merged[cid] = {
                **r,
                "vector_score": 0.0,
                "text_score": r["score"],
                "bfs_score": 0.0,
            }

    if bfs_results:
        max_shared = max((r["score"] for r in bfs_results), default=0) or 1
        for r in bfs_results:
            cid = r["chunk_id"]
            normalized = r["score"] / max_shared
            if cid in merged:
                merged[cid]["bfs_score"] = normalized
            else:
                merged[cid] = {
                    **r,
                    "vector_score": 0.0,
                    "text_score": 0.0,
                    "bfs_score": normalized,
                }

    results = []
    for entry in merged.values():
        d = entry.get("date")
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace("Z", "+00:00"))
        if d is None:
            d = now

        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)

        age_days = (now - d).total_seconds() / 86400.0

        relevance = (
            vector_weight * entry["vector_score"]
            + text_weight * entry["text_score"]
            + bfs_weight * entry["bfs_score"]
        )
        recency = max(
            recency_floor, math.exp(-0.693 * age_days / recency_half_life_days)
        )
        entry["relevance_score"] = relevance
        entry["recency_score"] = recency
        entry["final_score"] = relevance * recency
        results.append(entry)

    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results
