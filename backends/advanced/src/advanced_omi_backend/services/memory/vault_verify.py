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

from .vault_scaffold import VaultPathError, safe_vault_relative_path

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

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


@dataclass(frozen=True)
class Finding:
    """One violated rule, phrased so a model can act on it."""

    path: str
    rule: str
    detail: str

    def render(self) -> str:
        return f"- {self.path} [{self.rule}]: {self.detail}"


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


def verify_vault_changes(
    root: Path,
    before: Mapping[str, str],
    *,
    required: Sequence[str] = (),
    forbidden_folders: Sequence[str] = (),
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

    Case collisions are checked across the whole vault rather than only the changed
    set: the offending pair is one new note plus one that was already there, and only
    the pair is meaningful.
    """

    after = _markdown_files(root)
    findings: List[Finding] = []
    forbidden = tuple(f"{folder.rstrip('/')}/" for folder in forbidden_folders)

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

        reason = illegal_path_reason(rel)
        if reason:
            findings.append(Finding(rel, "illegal_path", reason))

        reason = non_person_note_reason(rel)
        if reason:
            findings.append(Finding(rel, "not_a_person", reason))

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
