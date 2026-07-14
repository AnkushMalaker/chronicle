"""String-replace edit engine for vault notes.

Reimplements the edit semantics used by coding-agent harnesses (pi, Claude Code):
each edit is an exact ``old_text`` → ``new_text`` replacement, matched against the
*original* file content, required to be unique, with helpful error messages that let
the model self-correct. Falls back to whitespace/punctuation-normalised matching when
the exact match fails (smart quotes, unicode dashes/spaces, trailing whitespace).

The helpful-error design is the valuable part: when the model's ``old_text`` does not
match or is ambiguous, the returned message tells it exactly how to fix the call.
"""

import re
from dataclasses import dataclass
from typing import List, Tuple


class EditError(Exception):
    """Raised when an edit cannot be applied. The message is sent back to the model."""


@dataclass
class Edit:
    old_text: str
    new_text: str


# --- fuzzy normalisation (mirrors pi's normalizeForFuzzyMatch) ---------------

_SMART_QUOTES = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
}
_DASHES = {c: "-" for c in "‐‑‒–—―−"}
_SPACES = {c: " " for c in "            　"}
_TRANSLATE = {ord(k): v for k, v in {**_SMART_QUOTES, **_DASHES, **_SPACES}.items()}


def _normalize(text: str) -> str:
    text = text.translate(_TRANSLATE)
    # strip trailing whitespace per line
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _find_unique(haystack: str, needle: str) -> Tuple[int, int]:
    """Return (start, length) of the single match of ``needle`` in ``haystack``.

    Tries exact match first, then a normalised match. Returns the span in terms of
    the *original* ``haystack`` indices. Raises with the occurrence count if the match
    is missing or ambiguous (caller turns this into a model-facing message).
    """
    count = haystack.count(needle)
    if count == 1:
        start = haystack.index(needle)
        return start, len(needle)
    if count > 1:
        raise _Ambiguous(count)
    if count == 0:
        # fuzzy: normalise both sides and map back via a regex over the original
        norm_needle = _normalize(needle)
        norm_hay = _normalize(haystack)
        ncount = norm_hay.count(norm_needle)
        if ncount == 1:
            # Build a tolerant regex from the needle: any run of the normalised
            # chars matches the corresponding original chars. Cheap approach:
            # locate by re-scanning original lines.
            span = _map_fuzzy_span(haystack, needle)
            if span is not None:
                return span
        if ncount > 1:
            raise _Ambiguous(ncount)
    raise _NoMatch()


def _map_fuzzy_span(haystack: str, needle: str):
    """Best-effort: find the original-text span whose normalised form == needle's."""
    norm_needle = _normalize(needle)
    # slide a window sized to the needle (+/- slack for collapsed whitespace)
    n = len(needle)
    for width in (n, n + 2, n + 5, n + 10):
        for i in range(0, max(0, len(haystack) - width) + 1):
            chunk = haystack[i : i + width]
            if _normalize(chunk) == norm_needle:
                return i, width
    return None


class _NoMatch(Exception):
    pass


class _Ambiguous(Exception):
    def __init__(self, count: int):
        self.count = count


def apply_edits(content: str, edits: List[Edit], path: str) -> str:
    """Apply all edits against the original ``content`` and return the new content.

    All edits are matched against the original (not incrementally), required unique,
    and checked for overlap. Raises :class:`EditError` with a self-correction hint.
    """
    if not edits:
        raise EditError(f"No edits provided for {path}.")

    spans: List[Tuple[int, int, str]] = []  # (start, length, new_text)
    for idx, edit in enumerate(edits):
        label = "" if len(edits) == 1 else f"edits[{idx}]"
        if not edit.old_text:
            where = f"{label}.old_text" if label else "old_text"
            raise EditError(f"{where} must not be empty in {path}.")
        try:
            start, length = _find_unique(content, edit.old_text)
        except _NoMatch:
            what = label or "the exact text"
            raise EditError(
                f"Could not find {what} in {path}. The old text must match exactly "
                f"including all whitespace and newlines. If it matched when you read "
                f"the note, the note was changed by a concurrent writer: read_note it "
                f"again and re-apply only your SMALLEST targeted edit against the "
                f"current content — do NOT re-write or re-append whole sections."
            )
        except _Ambiguous as a:
            what = label or "the text"
            unit = f"{label}" if label else "The text"
            raise EditError(
                f"Found {a.count} occurrences of {what} in {path}. {unit} must be "
                f"unique. Provide more surrounding context to make it unique."
            )
        spans.append((start, length, edit.new_text))

    # reject overlaps (compare against original indices)
    ordered = sorted(range(len(spans)), key=lambda i: spans[i][0])
    for a, b in zip(ordered, ordered[1:]):
        sa, la, _ = spans[a]
        sb, _, _ = spans[b]
        if sa + la > sb:
            raise EditError(
                f"edits[{a}] and edits[{b}] overlap in {path}. Merge them into one "
                f"edit or target disjoint regions."
            )

    # apply right-to-left so earlier indices stay valid
    new_content = content
    for start, length, new_text in sorted(spans, key=lambda s: s[0], reverse=True):
        new_content = new_content[:start] + new_text + new_content[start + length :]

    if new_content == content:
        raise EditError(
            f"No changes made to {path}. The replacement produced identical content; "
            f"check for special characters or that the text exists as expected."
        )
    return new_content
