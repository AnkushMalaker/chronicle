"""Deterministic duplicate-person suggestions and durable identity annotations."""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

from .person_merge import (
    PersonMergeError,
    PersonMergeStale,
    _as_list,
    _atomic_write,
    _join_frontmatter,
    _linked_person_names,
    _resolve_person,
    _section_bullets,
    _sha256,
    _split_frontmatter,
)
from .vault_lock import VaultLockTimeout, vault_note_lock

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
_CONVERSATION_RE = re.compile(r"Conversations/([0-9a-f-]{36})", re.IGNORECASE)
_PHOTO_RE = re.compile(r"_media/([^\]|]+)", re.IGNORECASE)
_IGNORED_CONTEXT_LINKS = {
    "people",
    "conversations.base",
    "conversations",
}


@dataclass
class PersonRecord:
    name: str
    path: str
    text: str
    content_hash: str
    aliases: set[str]
    distinct_from: set[str]
    org: str
    role: str
    links: set[str]
    conversations: set[str]
    photos: set[str]
    snippets: list[str]


@dataclass
class IdentityChangeResult:
    action_id: str
    person_a: str
    person_b: str
    decision: str
    changed_paths: list[str]
    before: dict[str, str]
    after: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "person_a": self.person_a,
            "person_b": self.person_b,
            "decision": self.decision,
            "changed_paths": self.changed_paths,
        }


def _normalise_name(name: str) -> str:
    return "".join(_TOKEN_RE.findall(name.casefold()))


def _tokens(name: str) -> set[str]:
    return set(_TOKEN_RE.findall(name.casefold()))


def _edit_distance(left: str, right: str) -> int:
    left = _normalise_name(left)
    right = _normalise_name(right)
    row = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        next_row = [index]
        for right_index, right_char in enumerate(right, 1):
            next_row.append(
                min(
                    next_row[-1] + 1,
                    row[right_index] + 1,
                    row[right_index - 1] + (left_char != right_char),
                )
            )
        row = next_row
    return row[-1]


def _plain_value(value: Any) -> str:
    return str(value).strip().casefold() if value else ""


def _aliases(value: Any) -> set[str]:
    result: set[str] = set()
    for item in _as_list(value):
        result.update(_linked_person_names(item))
    return result


def _record(path: Path, root: Path) -> PersonRecord:
    text = path.read_text(encoding="utf-8")
    frontmatter, _ = _split_frontmatter(text)
    links = {
        link.strip().casefold()
        for link in _LINK_RE.findall(text)
        if link.strip().casefold() not in _IGNORED_CONTEXT_LINKS
        and not link.startswith(("../", "Conversations/"))
    }
    snippets = [
        bullet.strip().lstrip("-").strip()
        for bullet in _section_bullets(text, "About")[:2]
    ]
    return PersonRecord(
        name=path.stem,
        path=path.relative_to(root).as_posix(),
        text=text,
        content_hash=_sha256(text),
        aliases=_aliases(frontmatter.get("aliases")),
        distinct_from=_linked_person_names(frontmatter.get("distinct_from")),
        org=_plain_value(frontmatter.get("org")),
        role=_plain_value(frontmatter.get("role")),
        links=links,
        conversations=set(_CONVERSATION_RE.findall(text)),
        photos={photo.casefold() for photo in _PHOTO_RE.findall(text)},
        snippets=snippets,
    )


def _pair_revision(left: PersonRecord, right: PersonRecord) -> str:
    payload = {
        "people": sorted(
            [(left.path, left.content_hash), (right.path, right.content_hash)]
        )
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _score_pair(left: PersonRecord, right: PersonRecord) -> tuple[int, list[str]]:
    left_name = _normalise_name(left.name)
    right_name = _normalise_name(right.name)
    shorter = min(len(left_name), len(right_name))
    distance = _edit_distance(left.name, right.name)
    similarity = SequenceMatcher(None, left_name, right_name).ratio()
    score = 0
    identity_signal = False
    reasons: list[str] = []

    if right.name.casefold() in left.aliases or left.name.casefold() in right.aliases:
        score += 100
        identity_signal = True
        reasons.append("one name is already an alias of the other")
    elif left_name == right_name:
        score += 90
        identity_signal = True
        reasons.append("names match after normalization")

    shared_photos = left.photos & right.photos
    if shared_photos:
        score += 90
        identity_signal = True
        reasons.append("same person photo")
    elif left.photos and right.photos:
        score -= 25

    if shorter >= 4 and distance == 1:
        score += 45
        identity_signal = True
        reasons.append("names differ by one character")
    elif shorter >= 6 and distance == 2:
        score += 25
        identity_signal = True
        reasons.append("names differ by two characters")

    if similarity >= 0.88:
        score += 25
        identity_signal = True
        reasons.append("very similar spelling")
    elif similarity >= 0.80:
        score += 15
        identity_signal = True
        reasons.append("similar spelling")

    left_tokens = _tokens(left.name)
    right_tokens = _tokens(right.name)
    if (
        left_tokens
        and right_tokens
        and left_tokens != right_tokens
        and (left_tokens < right_tokens or right_tokens < left_tokens)
        and shorter >= 4
    ):
        score += 25
        identity_signal = True
        reasons.append("one name appears to be a fuller form")

    shared_links = left.links & right.links
    if shared_links:
        score += min(18, len(shared_links) * 6)
        reasons.append(f"shared context in {len(shared_links)} linked note(s)")

    shared_conversations = left.conversations & right.conversations
    if shared_conversations:
        score += min(24, len(shared_conversations) * 12)
        reasons.append(f"same source conversation ({len(shared_conversations)})")

    if left.org and left.org == right.org:
        score += 15
        reasons.append("same organization")
    if left.role and left.role == right.role:
        score += 8
        reasons.append("same role")
    if not identity_signal:
        return 0, []
    return score, reasons


class PersonIdentityService:
    """Read identity candidates and write symmetric distinct-person decisions."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def suggestions(self, limit: int = 20, min_score: int = 40) -> list[dict[str, Any]]:
        people_dir = self.root / "People"
        if not people_dir.is_dir():
            return []
        records = [_record(path, self.root) for path in sorted(people_dir.glob("*.md"))]
        suggestions = []
        for left, right in combinations(records, 2):
            if (
                right.name.casefold() in left.distinct_from
                or left.name.casefold() in right.distinct_from
            ):
                continue
            score, reasons = _score_pair(left, right)
            if score < min_score:
                continue
            suggestions.append(
                {
                    "pair_id": _sha256(
                        "\0".join(sorted([left.name.casefold(), right.name.casefold()]))
                    )[:16],
                    "revision": _pair_revision(left, right),
                    "score": score,
                    "reasons": reasons,
                    "person_a": {
                        "name": left.name,
                        "path": left.path,
                        "hash": left.content_hash,
                        "snippets": left.snippets,
                    },
                    "person_b": {
                        "name": right.name,
                        "path": right.path,
                        "hash": right.content_hash,
                        "snippets": right.snippets,
                    },
                }
            )
        suggestions.sort(
            key=lambda item: (
                -item["score"],
                item["person_a"]["name"].casefold(),
                item["person_b"]["name"].casefold(),
            )
        )
        return suggestions[:limit]

    def set_distinct(
        self,
        person_a: str,
        person_b: str,
        *,
        distinct: bool,
        revision: Optional[str] = None,
    ) -> IdentityChangeResult:
        try:
            with vault_note_lock(self.root.name):
                return self._set_distinct_locked(
                    person_a, person_b, distinct=distinct, revision=revision
                )
        except VaultLockTimeout as exc:
            raise PersonMergeError("The vault is busy. Retry shortly.") from exc

    def _set_distinct_locked(
        self,
        person_a: str,
        person_b: str,
        *,
        distinct: bool,
        revision: Optional[str],
    ) -> IdentityChangeResult:
        path_a = _resolve_person(self.root, person_a)
        path_b = _resolve_person(self.root, person_b)
        if path_a == path_b:
            raise PersonMergeError(
                "A person cannot be marked distinct from themselves."
            )
        record_a = _record(path_a, self.root)
        record_b = _record(path_b, self.root)
        if revision and revision != _pair_revision(record_a, record_b):
            raise PersonMergeStale(
                "One of these people changed after the suggestion was shown. Review again."
            )

        new_a = self._update_distinct(record_a, record_b.name, distinct)
        new_b = self._update_distinct(record_b, record_a.name, distinct)
        before = {record_a.path: record_a.text, record_b.path: record_b.text}
        after = {record_a.path: new_a, record_b.path: new_b}
        changed = [path for path in after if after[path] != before[path]]
        try:
            for path in changed:
                _atomic_write(self.root / path, after[path])
        except Exception:
            for path, content in before.items():
                _atomic_write(self.root / path, content)
            raise
        return IdentityChangeResult(
            action_id=str(uuid.uuid4()),
            person_a=record_a.name,
            person_b=record_b.name,
            decision="distinct" if distinct else "clear_distinct",
            changed_paths=sorted(changed),
            before=before,
            after=after,
        )

    def _update_distinct(
        self, record: PersonRecord, other_name: str, distinct: bool
    ) -> str:
        frontmatter, body = _split_frontmatter(record.text)
        values = _as_list(frontmatter.get("distinct_from"))
        filtered = [
            value
            for value in values
            if other_name.casefold() not in _linked_person_names(value)
        ]
        if distinct:
            filtered.append(f"[[{other_name}]]")
        frontmatter["distinct_from"] = filtered
        if "updated" in frontmatter:
            frontmatter["updated"] = date.today().isoformat()
        return _join_frontmatter(frontmatter, body)
