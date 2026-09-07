"""Vault correctness rules, decoupled from the tool boundary.

These checks used to exist only *inside* ``VaultTools`` mutators, which meant an agent
could only be held to them by routing every write through Chronicle's own tools. That
forced us to re-implement file primitives too — and our ``read_note`` had no size bound,
so reading one oversized note blew the model's whole context.

Owning the rules here separates the two concerns. The same functions are used:

- **pre-write**, by ``VaultTools`` (which imports from this module), so the direct and Pi
  write paths still fail a bad mutation at the boundary where it is cheapest to fix;
- **post-write**, by :func:`verify_vault_changes`, which diffs the vault against a
  pre-run snapshot. That result is offered to the agent as a ``verify_vault`` tool so it
  can self-correct inside its own run, and re-run server-side as a gate so correctness
  does not depend on the agent choosing to ask.

A :class:`Finding` is addressed to a model: ``detail`` says how to fix it, not merely
what is wrong.

Deliberately **not** covered here: the conversation-note shape. That check
(``conversation_note.canonicalize_conversation_note``) rewrites frontmatter from trusted
source values — ``conversation_id``, ``date``, ``duration_minutes`` — which a generic
vault diff does not have. It stays on its existing provider-owned path.
"""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import yaml

from .vault_scaffold import VaultPathError, safe_vault_relative_path

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_DAY_DIGEST_RANGE_RE = re.compile(r"^###\s+(\d{2}:\d{2}–\d{2}:\d{2})\s+·", re.MULTILINE)
_DAY_NOTE_RANGE_RE = re.compile(
    r"^-\s+(?:\*\*)?(\d{2}:\d{2}–\d{2}:\d{2})(?:\*\*)?\s+·",
    re.MULTILINE,
)

# Long-lived structured notes carry a stable spine plus the aggregation embed that
# auto-lists their conversations. A note missing either is malformed forever.
NEW_NOTE_SCHEMA: Dict[str, Dict[str, Any]] = {
    "People": {
        "sections": ("about", "conversations", "mentions"),
        "embed": "![[Conversations.base#Person]]",
    },
    "Topics": {
        "sections": ("about", "conversations"),
        "embed": "![[Conversations.base#Topic]]",
    },
}

_UNKNOWN_SPEAKER_RE = re.compile(r"unknown speaker(?:\s+\d+)?", re.IGNORECASE)
_SYSTEM_CONTENT_FOLDERS = frozenset(
    {"Conversations", "Daily", "Manual Memories", "People", "Templates", "Topics"}
)
_TOPIC_FACT_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TOPIC_FACT_STOPWORDS = frozenset(
    {
        "about",
        "and",
        "are",
        "for",
        "from",
        "into",
        "that",
        "the",
        "this",
        "through",
        "using",
        "via",
        "with",
    }
)


def frontmatter_parse_error(content: str) -> str | None:
    """Return an actionable error when an apparent YAML frontmatter block is invalid."""

    if not content.startswith("---"):
        return None
    boundaries = list(re.finditer(r"^---\s*$", content, re.MULTILINE))
    if not boundaries or boundaries[0].start() != 0:
        return "YAML frontmatter opening delimiter must be the first line"
    if len(boundaries) < 2:
        return "YAML frontmatter has no closing `---` delimiter"
    raw = content[boundaries[0].end() : boundaries[1].start()]
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return f"invalid YAML frontmatter: {exc}"
    if parsed is not None and not isinstance(parsed, dict):
        return "YAML frontmatter must be a key/value mapping"
    return None


@dataclass(frozen=True)
class Finding:
    """One violated rule, phrased so a model can act on it."""

    path: str
    rule: str
    detail: str

    def render(self) -> str:
        return f"- {self.path} [{self.rule}]: {self.detail}"


@dataclass(frozen=True)
class TopicScopeOverlap:
    """A newly-created Topic whose facts are already carried by another Topic."""

    path: str
    other_path: str
    matched_bullets: int
    total_bullets: int


def section_counts(text: str) -> Counter:
    """Count each top-level ``## Section`` heading, case-insensitively."""

    return Counter(m.group(1).lower() for m in _H2_RE.finditer(text))


def new_duplicate_sections(before: str, after: str) -> List[str]:
    """Section headings that ``after`` duplicates and ``before`` did not.

    Compared against ``before`` so an edit to an already-duplicated note can still
    proceed — otherwise repairing one would be impossible.
    """

    bc = section_counts(before)
    ac = section_counts(after)
    return sorted(h for h, n in ac.items() if n > 1 and n > bc.get(h, 0))


def _meaningful_section_bodies(text: str, heading: str) -> tuple[str, ...]:
    """Return non-placeholder bodies of every matching top-level H2 section.

    A missing section, an empty section, and the canonical template's bare ``-``
    placeholder are semantically equivalent. This lets a day create a well-formed new
    Person note while still making any real ``Mentions`` content immutable.
    """

    matches = list(_H2_RE.finditer(text))
    wanted = heading.casefold()
    bodies: List[str] = []
    for index, match in enumerate(matches):
        if match.group(1).casefold() != wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lines = [line.rstrip() for line in text[match.end() : end].splitlines()]
        meaningful = [line for line in lines if line.strip() not in {"", "-"}]
        if meaningful:
            bodies.append("\n".join(meaningful))
    return tuple(bodies)


def changed_immutable_sections(
    rel: str,
    before: str,
    after: str,
    immutable_sections: Sequence[tuple[str, str]],
) -> List[str]:
    """Configured section names whose meaningful content changed in ``rel``."""

    parts = Path(rel).parts
    if len(parts) < 2:
        return []
    folder = parts[0].casefold()
    changed: List[str] = []
    for configured_folder, heading in immutable_sections:
        if folder != configured_folder.casefold():
            continue
        if _meaningful_section_bodies(before, heading) != _meaningful_section_bodies(
            after, heading
        ):
            changed.append(heading)
    return changed


def _section_bullets(text: str, heading: str) -> List[str]:
    """Fact bullets under one top-level H2 section, excluding template placeholders."""

    matches = list(_H2_RE.finditer(text))
    wanted = heading.casefold()
    bullets: List[str] = []
    for index, match in enumerate(matches):
        if match.group(1).casefold() != wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        for line in text[match.end() : end].splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and stripped[2:].strip():
                bullets.append(stripped[2:].strip())
    return bullets


def _topic_fact_tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOPIC_FACT_TOKEN_RE.findall(text.casefold())
        if len(token) > 2 and token not in _TOPIC_FACT_STOPWORDS
    }


def _topic_scope_score(candidate: str, other: str) -> tuple[int, int]:
    """How many candidate About bullets are substantially contained by ``other``."""

    candidate_tokens = [
        tokens
        for bullet in _section_bullets(candidate, "About")
        if len(tokens := _topic_fact_tokens(bullet)) >= 6
    ]
    other_tokens = [
        tokens
        for bullet in _section_bullets(other, "About")
        if len(tokens := _topic_fact_tokens(bullet)) >= 6
    ]
    matched = 0
    for tokens in candidate_tokens:
        for comparison in other_tokens:
            intersection = len(tokens & comparison)
            if intersection >= 6 and intersection / len(tokens) >= 0.65:
                matched += 1
                break
    return matched, len(candidate_tokens)


def new_topic_scope_overlaps(
    before: Mapping[str, str], after: Mapping[str, str]
) -> List[TopicScopeOverlap]:
    """Find new Topic notes whose About section substantially duplicates one peer.

    One related fact does not collapse two legitimate subjects. A new note is rejected
    only when at least two, and at least three quarters, of its substantive bullets are
    already contained by one other Topic. This caught the measured ``Policy Store``
    note (4/4 bullets repeated ``Agent Control``) without collapsing a related load-test
    note whose remaining facts were distinct.
    """

    topics = {
        rel: content
        for rel, content in after.items()
        if len(Path(rel).parts) == 2 and Path(rel).parts[0].casefold() == "topics"
    }
    new_paths = {rel for rel in topics if rel not in before}
    candidates: Dict[str, TopicScopeOverlap] = {}
    for rel in sorted(new_paths):
        best: TopicScopeOverlap | None = None
        for other_rel, other_content in topics.items():
            if other_rel == rel:
                continue
            matched, total = _topic_scope_score(topics[rel], other_content)
            if total < 2 or matched < 2 or matched / total < 0.75:
                continue
            overlap = TopicScopeOverlap(rel, other_rel, matched, total)
            if best is None or (matched / total, matched, other_rel) > (
                best.matched_bullets / best.total_bullets,
                best.matched_bullets,
                best.other_path,
            ):
                best = overlap
        if best is not None:
            candidates[rel] = best

    # Two identical notes created in one run qualify against each other. Keep one
    # deterministic canonical candidate and report only the casefold-later path.
    overlaps: List[TopicScopeOverlap] = []
    for rel, overlap in sorted(candidates.items()):
        reciprocal = candidates.get(overlap.other_path)
        if reciprocal is not None and reciprocal.other_path == rel:
            if rel.casefold() < overlap.other_path.casefold():
                continue
        overlaps.append(overlap)
    return overlaps


def new_note_schema_problems(rel: str, content: str) -> List[str]:
    """Human-readable defects in a new People/Topic note, or an empty list."""

    parts = Path(rel).parts
    if len(parts) != 2:
        return []
    schema = NEW_NOTE_SCHEMA.get(parts[0])
    if schema is None:
        return []

    counts = section_counts(content)
    sections: Iterable[str] = schema["sections"]  # type: ignore[assignment]
    embed = str(schema["embed"])
    problems: List[str] = []
    missing = [name for name in sections if counts.get(name, 0) == 0]
    if missing:
        problems.append(
            "missing section(s) " + ", ".join(f"'## {n.title()}'" for n in missing)
        )
    if embed not in content:
        problems.append(f"missing exact embed {embed!r}")
    return problems


def non_person_note_reason(rel: str) -> str:
    """Why this path must not be a person note, or ``""`` if it is fine."""

    parts = Path(rel).parts
    if len(parts) != 2 or parts[0] != "People":
        return ""
    stem = Path(rel).stem
    if _UNKNOWN_SPEAKER_RE.fullmatch(stem):
        return (
            "'Unknown Speaker N' is a diarization placeholder, not a person. Delete this "
            "note and remove any [[wikilink]] to it."
        )
    if stem.casefold() == "hermes":
        return (
            "Hermes is Chronicle's assistant, not a person. Delete this note and record "
            "it as the topic Topics/Hermes.md instead."
        )
    return ""


def illegal_path_reason(rel: str) -> str:
    """Why this vault-relative path is not a legal note path, or ``""``."""

    try:
        safe = safe_vault_relative_path(rel)
    except VaultPathError:
        return "path escapes the vault or contains illegal characters."
    parts = list(Path(safe).parts)
    if len(parts) > 2:
        return (
            "notes live at <Folder>/<Title>.md, one folder deep. A '/' in a title mints "
            "nested folders — rename it without '/'."
        )
    dirs = parts[:-1]
    stem = parts[-1][: -len(".md")] if parts[-1].endswith(".md") else parts[-1]
    if not stem.strip() or any(not d.strip() for d in dirs):
        return "empty folder or note title."
    if stem != stem.strip() or any(d != d.strip() for d in dirs):
        return (
            "leading/trailing whitespace in a folder or title breaks Windows and "
            "Syncthing; rename it without the surrounding spaces."
        )
    return ""


def root_note_role_reason(
    root: Path, rel: str, before: str | None, content: str
) -> str:
    """Why a changed root Markdown note is not a valid category hub.

    Content notes live one folder deep.  Root Markdown is reserved for the thin hub
    notes that make category wikilinks resolve and embed their matching Obsidian Base.
    Organic categories remain open-ended, but they must be created as the complete
    template/base/hub bundle rather than by dropping an entity or topic at the root.
    """

    path = Path(rel)
    if len(path.parts) != 1 or path.suffix != ".md":
        return ""

    category = path.stem
    if before is not None:
        return (
            "root Markdown files are category hubs, not captured-content notes. Do not "
            "edit the hub; put durable content in its category folder (for a topic, "
            f"`Topics/{category}.md`)."
        )

    template = root / "Templates" / f"{category} Template.md"
    base = root / "Templates" / "Bases" / f"{category}.base"
    is_complete_hub = (
        template.is_file()
        and base.is_file()
        and f"# {category}" in content
        and f"![[{category}.base]]" in content
    )
    if is_complete_hub:
        return ""
    return (
        "root Markdown files are reserved for category hubs created as a matching "
        "template/base/hub bundle. If this is a topic, move it to "
        f"`Topics/{category}.md`; if it is a new recurring kind of thing, create the "
        "category first and file the note under `<Category>/<Title>.md`."
    )


def _markdown_files(root: Path) -> Dict[str, str]:
    """Every readable ``*.md`` in the vault, keyed by vault-relative POSIX path."""

    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def new_category_creations(
    before: Mapping[str, str], after: Mapping[str, str]
) -> Dict[str, str]:
    """New category names mapped to one representative changed path.

    A complete organic category normally introduces a root hub, template and Base.
    The Base is not Markdown, so the diff cannot rely on seeing all three. Detect the
    new hub first, then also catch an agent that skipped the hub and wrote directly to
    a previously unknown top-level folder or emitted only a category template.
    """

    created: Dict[str, str] = {}
    for rel, content in sorted(after.items()):
        if rel in before or before.get(rel) == content:
            continue
        path = Path(rel)
        if len(path.parts) == 1 and path.suffix.casefold() == ".md":
            created[path.stem] = rel

    for rel, content in sorted(after.items()):
        if rel in before or before.get(rel) == content:
            continue
        path = Path(rel)
        if len(path.parts) != 2:
            continue
        folder = path.parts[0]
        if folder == "Templates" and path.name.endswith(" Template.md"):
            category = path.name[: -len(" Template.md")]
            if f"{category}.md" not in before:
                created.setdefault(category, rel)
            continue
        if folder in _SYSTEM_CONTENT_FOLDERS or f"{folder}.md" in before:
            continue
        created.setdefault(folder, rel)
    return created


def verify_day_episode_ranges(note_path: Path, day_digest: str) -> List[Finding]:
    """Require the Daily episode index to mirror the active timeline exactly.

    A day can be analysed again after it was already written. The write agent used to
    interpret "add only what is missing" literally: it appended a newly discovered
    episode but retained stale time ranges for every existing episode. The write then
    looked healthy even though the vault no longer represented the active run.

    The episode index is a source-backed Chronicle contract rather than model-authored
    prose: one concise ordered bullet per supplied episode, with the exact range
    selected by segmentation. Detailed summaries stay on TimelineEpisode and raw
    transcripts remain outside the vault.
    """

    expected = _DAY_DIGEST_RANGE_RE.findall(day_digest or "")
    try:
        note = note_path.read_text(encoding="utf-8")
    except OSError:
        note = ""

    episodes_heading = re.search(r"^##\s+Episodes\s*$", note, re.MULTILINE)
    if episodes_heading is None:
        section = ""
    else:
        section_start = episodes_heading.end()
        next_heading = _H2_RE.search(note, section_start)
        section_end = next_heading.start() if next_heading else len(note)
        section = note[section_start:section_end]
    actual = _DAY_NOTE_RANGE_RE.findall(section)

    if actual == expected:
        return []

    rel = "/".join(note_path.parts[-2:])
    return [
        Finding(
            rel,
            "episode_ranges",
            "replace only the `## Episodes` section with exactly one chronological "
            "bullet per supplied day episode, using each source range verbatim and "
            "removing every stale or duplicate bullet. "
            f"Expected {len(expected)} range(s): {', '.join(expected) or '(none)'}. "
            f"Found {len(actual)}: {', '.join(actual) or '(none)' }.",
        )
    ]


def verify_vault_changes(
    root: Path,
    before: Mapping[str, str],
    *,
    required: Sequence[str] = (),
    forbidden_folders: Sequence[str] = (),
    immutable_sections: Sequence[tuple[str, str]] = (),
    forbid_new_categories: bool = False,
) -> List[Finding]:
    """Check what changed in ``root`` since the ``before`` snapshot.

    ``before`` maps vault-relative path to content — the shape both
    ``CodexMemoryAgent._snapshot`` and ``ChronicleMemoryProvider._vault_note_set``
    already produce, so callers have it in hand.

    ``required`` names notes this run must have created or edited. A day write is the
    case that needs it: DeepSeek V4 Pro updated two People notes for 2026-08-06, never
    wrote `Daily/2026-08-06.md`, and stopped after ten rounds with no error, no
    truncation, and no stall — it simply believed it was finished. Reporting that as a
    finding lets the agent fix it mid-run instead of the provider discovering it after
    the process has exited.

    ``forbidden_folders`` names folders this kind of write must not touch. A day write
    must not create anything under ``Conversations/`` — that folder is one note per real
    conversation, keyed by conversation_id — but Qwen3.6 wrote a whole
    ``Conversations/ads-standup-2026-08-06.md`` from an episode, minting an id matching
    no conversation and shadowing the real note the conversation path would write.

    ``immutable_sections`` scopes section ownership by ``(folder, heading)``. Day
    writes use it for ``People/## Mentions``: Daily/Timeline is the chronological
    record, while People notes retain only durable facts in ``About``.

    Case collisions are checked across the whole vault rather than only the changed
    set: the offending pair is one new note plus one that was already there, and only
    the pair is meaningful.
    """

    after = _markdown_files(root)
    findings: List[Finding] = []
    forbidden = tuple(f"{folder.rstrip('/')}/" for folder in forbidden_folders)
    topic_overlaps = {
        overlap.path: overlap for overlap in new_topic_scope_overlaps(before, after)
    }
    new_category_paths = (
        {
            path: category
            for category, path in new_category_creations(before, after).items()
        }
        if forbid_new_categories
        else {}
    )

    for rel in required:
        if rel in after and after[rel] != before.get(rel):
            continue
        findings.append(
            Finding(
                rel,
                "record_missing",
                "this run has not written it. That note is the record itself — edits to "
                "People/Topic notes do not stand in for it. Create it from the source, "
                "or edit_section it to add what is missing, before you finish.",
            )
        )

    for rel, content in sorted(after.items()):
        was = before.get(rel)
        if was == content:
            continue

        if forbidden and rel.startswith(forbidden):
            folder = rel.split("/", 1)[0]
            findings.append(
                Finding(
                    rel,
                    "forbidden_folder",
                    f"this kind of write must not touch {folder}/. Delete this note and "
                    f"record the same material where it belongs — the day note for what "
                    f"happened, and People/Topic notes for durable facts.",
                )
            )

        category = new_category_paths.get(rel)
        if category is not None:
            findings.append(
                Finding(
                    rel,
                    "new_category",
                    f"a day write cannot invent the '{category}' category schema. "
                    "Delete the new hub/template/Base bundle and its new category "
                    "notes. Keep the observation in Daily, link it unresolved, or "
                    "record durable facts in an already-existing category.",
                )
            )

        for heading in changed_immutable_sections(
            rel,
            was or "",
            content,
            immutable_sections,
        ):
            findings.append(
                Finding(
                    rel,
                    "immutable_section",
                    f"this write type cannot modify `## {heading}` in People notes. "
                    "Daily/Timeline owns chronological activity; restore this section "
                    "exactly and put only genuinely durable personal facts in "
                    "`## About`.",
                )
            )

        overlap = topic_overlaps.get(rel)
        if overlap is not None:
            findings.append(
                Finding(
                    rel,
                    "topic_scope_overlap",
                    f"{overlap.matched_bullets}/{overlap.total_bullets} substantive "
                    f"`## About` bullets substantially repeat "
                    f"{overlap.other_path}. Keep one canonical Topic: move only any "
                    "genuinely unique facts into that note, then remove this newly "
                    "created overlapping note.",
                )
            )

        reason = illegal_path_reason(rel)
        if reason:
            findings.append(Finding(rel, "illegal_path", reason))

        reason = root_note_role_reason(root, rel, was, content)
        if reason:
            findings.append(Finding(rel, "root_note_role", reason))

        reason = non_person_note_reason(rel)
        if reason:
            findings.append(Finding(rel, "not_a_person", reason))

        reason = frontmatter_parse_error(content)
        if reason:
            findings.append(
                Finding(
                    rel,
                    "invalid_frontmatter",
                    f"{reason}. Repair the YAML before finishing; properties with "
                    "multiple values must use a YAML list such as "
                    '`author: ["[[A]]", "[[B]]"]`.',
                )
            )

        dupes = new_duplicate_sections(was or "", content)
        if dupes:
            pretty = ", ".join(f"'## {h}'" for h in dupes)
            findings.append(
                Finding(
                    rel,
                    "duplicate_section",
                    f"section heading(s) {pretty} now appear more than once. A note "
                    f"carries each section exactly once — remove the repeated copy, "
                    f"keeping the facts from both.",
                )
            )

        if was is None:
            problems = new_note_schema_problems(rel, content)
            if problems:
                template = "Person" if rel.startswith("People/") else "Topic"
                findings.append(
                    Finding(
                        rel,
                        "note_schema",
                        f"{'; '.join(problems)}. Fill it from "
                        f"Templates/{template} Template.md, preserving every required "
                        f"section and copying the embed line verbatim.",
                    )
                )
            parts = Path(rel).parts
            if (
                len(parts) == 2
                and parts[0] in NEW_NOTE_SCHEMA
                and not _section_bullets(content, "About")
            ):
                findings.append(
                    Finding(
                        rel,
                        "empty_semantic_note",
                        "this newly-created note has no substantive `## About` fact. "
                        "An empty scaffold is not durable memory: add only a supported, "
                        "reusable fact from the source, or delete the note and leave the "
                        "name as an unresolved wikilink.",
                    )
                )

    by_fold: Dict[str, List[str]] = {}
    for rel in after:
        by_fold.setdefault(rel.casefold(), []).append(rel)
    for variants in by_fold.values():
        if len(variants) < 2:
            continue
        touched = [v for v in sorted(variants) if before.get(v) != after.get(v)]
        if not touched:
            continue  # pre-existing collision, not something this run introduced
        findings.append(
            Finding(
                touched[0],
                "case_collision",
                f"{' and '.join(sorted(variants))} differ only by capitalisation. "
                f"Syncthing cannot represent both on macOS or Windows — merge their "
                f"content into the one that already existed and delete the other.",
            )
        )

    return findings


def render_findings(findings: List[Finding]) -> str:
    """Findings as the text an agent (or a repair round) is given."""

    if not findings:
        return "Vault verification passed: no problems found."
    return (
        f"Vault verification found {len(findings)} problem(s). Fix every one, then call "
        f"verify_vault again:\n" + "\n".join(f.render() for f in findings)
    )
