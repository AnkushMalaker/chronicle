"""Heading/block-ref *targeted* editing for vault notes (PROTOTYPE).

This is the structural counterpart to :mod:`edit_engine`'s exact ``old_text`` →
``new_text`` replacement. Instead of anchoring on a literal slice of the note body —
which goes stale the moment a concurrent writer touches that slice — an edit here
targets a note's *structure*: a ``## Heading`` or an Obsidian block reference
(``^block-id``), plus an operation (``append`` / ``prepend`` / ``replace``).

Why this exists
---------------
The memory agent's dominant edit is "append a genuinely-new bullet under ``## About``
and a dated line under ``## Mentions``" (see the agent system prompt). Expressed as an
``old_text`` edit, the model must paste the section's current tail verbatim as an
anchor — so when two same-user memory runs interleave, the anchor it read is no longer
present and the edit fails, forcing a read-retry loop (the root cause documented in the
memory-job-serialization work). A heading-targeted append needs *no* knowledge of the
section's current contents, so it survives concurrent edits that don't restructure the
note. It mirrors the Obsidian Local REST API ``PATCH``-with-target design the community
has converged on for AI agents.

This module is intentionally self-contained and dependency-free so it can be unit
tested in isolation and trialled alongside the existing ``edit_note`` tool.
"""

import re
from typing import List, Tuple

# Operations an edit may perform against a located target.
APPEND = "append"
PREPEND = "prepend"
REPLACE = "replace"
_OPERATIONS = (APPEND, PREPEND, REPLACE)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


class SectionEditError(Exception):
    """Raised when a targeted edit cannot be applied; message is sent back to the model."""


def _split_keepends(content: str) -> List[str]:
    """Split into lines preserving terminators (so re-joining is loss-free)."""
    return content.splitlines(keepends=True)


def _norm_heading(target: str) -> str:
    """Strip leading ``#`` markers and surrounding whitespace from a heading target."""
    return target.lstrip("#").strip()


def _ensure_block(text: str) -> str:
    """Normalise inserted text to end with exactly one newline (never zero, never two)."""
    return text.rstrip("\n") + "\n"


def _find_heading_lines(lines: List[str], heading: str) -> List[int]:
    """Return indices of every ``#..###### <heading>`` line, case-insensitively."""
    want = heading.casefold()
    hits = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m and m.group(2).casefold() == want:
            hits.append(i)
    return hits


def _heading_level(line: str) -> int:
    m = _HEADING_RE.match(line.rstrip("\n"))
    return len(m.group(1)) if m else 0


def _section_body_span(lines: List[str], heading_idx: int) -> Tuple[int, int]:
    """Return ``(body_start, body_end)`` line indices for the section opened at
    ``heading_idx``. The body runs from the line after the heading up to (but not
    including) the next heading whose level is <= this heading's level, or EOF.
    """
    level = _heading_level(lines[heading_idx])
    body_start = heading_idx + 1
    body_end = len(lines)
    for j in range(body_start, len(lines)):
        lvl = _heading_level(lines[j])
        if lvl and lvl <= level:
            body_end = j
            break
    return body_start, body_end


def _apply_to_heading(
    lines: List[str], heading: str, text: str, operation: str
) -> List[str]:
    hits = _find_heading_lines(lines, heading)
    if not hits:
        raise SectionEditError(
            f"No section heading matching '{heading}' found. Targets are matched on the "
            f"heading text without '#'. read_note the file to see its exact headings, or "
            f"create the section first."
        )
    if len(hits) > 1:
        raise SectionEditError(
            f"Found {len(hits)} headings matching '{heading}'; the target must be unique. "
            f"A structured note carries each section once — repair the duplicate, or "
            f"disambiguate by including the level (e.g. '### {heading}')."
        )
    h = hits[0]
    body_start, body_end = _section_body_span(lines, h)
    block = _ensure_block(text)

    if operation == PREPEND:
        # Insert immediately under the heading line, before existing body.
        return lines[: h + 1] + [block] + lines[h + 1 :]

    if operation == REPLACE:
        # Replace the whole section body, preserving one trailing blank if the
        # original had separation before the next heading.
        trailing = lines[body_end - 1] if body_end > body_start else ""
        keep_blank = (
            [trailing] if trailing.strip() == "" and body_end < len(lines) else []
        )
        return lines[: h + 1] + [block] + keep_blank + lines[body_end:]

    # APPEND: insert after the last non-blank line of the body, keeping any trailing
    # blank line(s) that separate this section from the next heading.
    last_content = body_start - 1
    for k in range(body_start, body_end):
        if lines[k].strip():
            last_content = k
    insert_at = last_content + 1
    return lines[:insert_at] + [block] + lines[insert_at:]


_BLOCK_ID_RE = re.compile(r"\^([A-Za-z0-9][A-Za-z0-9\-]*)\s*$")


def _find_block_lines(lines: List[str], block_id: str) -> List[int]:
    """Return indices of lines ending with the Obsidian block id ``^block_id``."""
    hits = []
    for i, line in enumerate(lines):
        m = _BLOCK_ID_RE.search(line.rstrip("\n"))
        if m and m.group(1) == block_id:
            hits.append(i)
    return hits


def _apply_to_block(
    lines: List[str], block_id: str, text: str, operation: str
) -> List[str]:
    hits = _find_block_lines(lines, block_id)
    if not hits:
        raise SectionEditError(
            f"No block reference '^{block_id}' found. Block ids are the '^id' marker at "
            f"the end of a line. read_note the file to confirm it exists."
        )
    if len(hits) > 1:
        raise SectionEditError(
            f"Found {len(hits)} lines ending with '^{block_id}'; a block id must be unique."
        )
    b = hits[0]
    block = _ensure_block(text)
    if operation == PREPEND:
        return lines[:b] + [block] + lines[b:]
    if operation == REPLACE:
        return lines[:b] + [block] + lines[b + 1 :]
    # APPEND: insert directly after the referenced block line.
    return lines[: b + 1] + [block] + lines[b + 1 :]


def apply_section_edit(
    content: str, target: str, text: str, operation: str = APPEND
) -> str:
    """Apply a structural edit and return the new content.

    ``target`` is either a heading (``"About"`` / ``"## About"`` / ``"### Summary"``) or
    an Obsidian block reference (``"^block-id"``). ``operation`` is one of ``append``
    (default), ``prepend``, or ``replace``. Raises :class:`SectionEditError` with a
    self-correction hint the model can act on.
    """
    if operation not in _OPERATIONS:
        raise SectionEditError(
            f"Unknown operation '{operation}': use one of {', '.join(_OPERATIONS)}."
        )
    if not text or not text.strip():
        raise SectionEditError("Refusing an empty insertion; provide text to add.")

    lines = _split_keepends(content)
    # Ensure the file ends with a newline so insertions never weld onto the last line.
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"

    if target.strip().startswith("^"):
        new_lines = _apply_to_block(lines, target.strip()[1:], text, operation)
    else:
        new_lines = _apply_to_heading(lines, _norm_heading(target), text, operation)

    new_content = "".join(new_lines)
    if new_content == content:
        raise SectionEditError(
            f"No change produced for target '{target}'. Check the operation and text."
        )
    return new_content
