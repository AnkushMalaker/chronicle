"""Deterministic, transactional-ish person-note merges for the Chronicle vault.

Identity resolution is intentionally outside this module: a human or agent decides
that two notes describe the same person.  This module only executes the mechanical
operation with fixed rules, a preview token, the per-user vault lock, and rollback on
ordinary failures.
"""

import hashlib
import io
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML

from .agent.section_edit import SectionEditError, apply_section_edit
from .vault_lock import VaultLockTimeout, vault_note_lock

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_MERGE_SECTIONS = ("About", "Mentions")
_SCALAR_IDENTITY_FIELDS = ("org", "role", "relationship", "location")


class PersonMergeError(Exception):
    """A person merge cannot be previewed or applied safely."""


class PersonMergeStale(PersonMergeError):
    """The vault changed after the caller read or previewed it."""


@dataclass(frozen=True)
class MetadataConflict:
    field: str
    source_value: Any
    target_value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "source_value": self.source_value,
            "target_value": self.target_value,
        }


@dataclass
class PersonMergePreview:
    source_name: str
    target_name: str
    source_path: str
    target_path: str
    source_hash: str
    target_hash: str
    plan_token: str
    facts_to_add: int
    duplicate_facts_skipped: int
    backlink_files: list[str]
    backlink_occurrences: int
    metadata_conflicts: list[MetadataConflict]
    _source_text: str = field(repr=False)
    _target_text: str = field(repr=False)
    _before: dict[str, str] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "target_name": self.target_name,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "source_hash": self.source_hash,
            "target_hash": self.target_hash,
            "plan_token": self.plan_token,
            "facts_to_add": self.facts_to_add,
            "duplicate_facts_skipped": self.duplicate_facts_skipped,
            "backlink_files": self.backlink_files,
            "backlink_occurrences": self.backlink_occurrences,
            "metadata_conflicts": [item.to_dict() for item in self.metadata_conflicts],
        }


@dataclass
class PersonMergeResult:
    action_id: str
    preview: PersonMergePreview
    changed_paths: list[str]
    before: dict[str, str]
    after: dict[str, Optional[str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            **self.preview.to_dict(),
            "changed_paths": self.changed_paths,
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or Path(cleaned).name != cleaned
        or "/" in cleaned
        or "\\" in cleaned
    ):
        raise PersonMergeError(
            "Person names must be plain note titles without slashes."
        )
    return cleaned


def _resolve_person(root: Path, name: str) -> Path:
    people = root / "People"
    wanted = f"{_validate_name(name)}.md".casefold()
    if not people.is_dir():
        raise PersonMergeError("The vault has no People folder.")
    matches = [path for path in people.glob("*.md") if path.name.casefold() == wanted]
    if not matches:
        raise PersonMergeError(f"People/{name}.md does not exist.")
    if len(matches) > 1:
        raise PersonMergeError(f"Multiple case-variant notes match People/{name}.md.")
    return matches[0]


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise PersonMergeError("Person note is missing YAML frontmatter.")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise PersonMergeError("Person note has malformed YAML frontmatter.")
    yaml = YAML(typ="rt")
    data = yaml.load(text[4:end]) or {}
    if not isinstance(data, dict):
        raise PersonMergeError("Person note frontmatter must be a mapping.")
    return data, text[end + 5 :]


def _join_frontmatter(data: dict[str, Any], body: str) -> str:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    stream = io.StringIO()
    yaml.dump(data, stream)
    return f"---\n{stream.getvalue()}---\n{body.lstrip()}"


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, list) else [value]


def _union_values(*collections: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for collection in collections:
        for value in collection:
            marker = str(value).strip().casefold()
            if marker and marker not in seen:
                result.append(value)
                seen.add(marker)
    return result


def _linked_person_names(value: Any) -> set[str]:
    """Return case-folded person titles from a frontmatter link/list value."""
    names: set[str] = set()
    for item in _as_list(value):
        text = str(item).strip()
        match = re.fullmatch(r"\[\[(?:People/)?([^\]|#]+)(?:[|#][^\]]*)?\]\]", text)
        title = match.group(1) if match else text
        if title:
            names.add(title.strip().casefold())
    return names


def _section_bullets(content: str, heading: str) -> list[str]:
    wanted = heading.casefold()
    found = False
    result: list[str] = []
    for line in content.splitlines():
        match = _H2_RE.match(line.rstrip())
        if match:
            found = match.group(1).casefold() == wanted
            continue
        stripped = line.strip()
        if found and stripped.startswith("-") and stripped.lstrip("-").strip():
            result.append(line.rstrip())
    return result


def _normalise_bullet(line: str) -> str:
    return " ".join(line.lstrip().lstrip("-").split()).casefold()


def _media_embeds(body: str) -> list[str]:
    prologue = body.split("\n## ", 1)[0]
    return [
        line.strip() for line in prologue.splitlines() if line.strip().startswith("![[")
    ]


def _add_media_embeds(body: str, embeds: list[str]) -> str:
    missing = [embed for embed in embeds if embed not in body]
    if not missing:
        return body
    block = "\n".join(missing) + "\n"
    return block + body.lstrip()


def _link_pattern(source_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?P<prefix>\[\[(?:People/)?)({re.escape(source_name)})"
        rf"(?P<suffix>(?:[#|][^\]]*)?\]\])",
        re.IGNORECASE,
    )


def _rewrite_links(text: str, source_name: str, target_name: str) -> tuple[str, int]:
    pattern = _link_pattern(source_name)
    return pattern.subn(
        lambda match: f"{match.group('prefix')}{target_name}{match.group('suffix')}",
        text,
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class PersonMergeService:
    """Preview and apply one deterministic person merge inside a vault root."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def preview(
        self,
        source_name: str,
        target_name: str,
        *,
        expected_source_hash: Optional[str] = None,
        expected_target_hash: Optional[str] = None,
    ) -> PersonMergePreview:
        source = _resolve_person(self.root, source_name)
        target = _resolve_person(self.root, target_name)
        if source == target:
            raise PersonMergeError("Source and target resolve to the same person note.")

        source_text = source.read_text(encoding="utf-8")
        target_text = target.read_text(encoding="utf-8")
        source_hash = _sha256(source_text)
        target_hash = _sha256(target_text)
        if expected_source_hash and expected_source_hash != source_hash:
            raise PersonMergeStale("The source note differs from the server copy.")
        if expected_target_hash and expected_target_hash != target_hash:
            raise PersonMergeStale("The target note differs from the server copy.")

        source_frontmatter, _ = _split_frontmatter(source_text)
        target_frontmatter, _ = _split_frontmatter(target_text)
        if target.stem.casefold() in _linked_person_names(
            source_frontmatter.get("distinct_from")
        ) or source.stem.casefold() in _linked_person_names(
            target_frontmatter.get("distinct_from")
        ):
            raise PersonMergeError(
                f"{source.stem} and {target.stem} are marked as separate people. "
                "Clear that identity annotation before merging them."
            )
        conflicts = []
        for key in _SCALAR_IDENTITY_FIELDS:
            source_value = source_frontmatter.get(key)
            target_value = target_frontmatter.get(key)
            if source_value and target_value and source_value != target_value:
                conflicts.append(MetadataConflict(key, source_value, target_value))

        facts_to_add = 0
        duplicate_facts = 0
        for heading in _MERGE_SECTIONS:
            existing = {
                _normalise_bullet(line)
                for line in _section_bullets(target_text, heading)
            }
            for bullet in _section_bullets(source_text, heading):
                if _normalise_bullet(bullet) in existing:
                    duplicate_facts += 1
                else:
                    facts_to_add += 1
                    existing.add(_normalise_bullet(bullet))

        backlink_files: list[str] = []
        backlink_occurrences = 0
        before: dict[str, str] = {
            source.relative_to(self.root).as_posix(): source_text,
            target.relative_to(self.root).as_posix(): target_text,
        }
        pattern = _link_pattern(source.stem)
        for path in sorted(self.root.rglob("*.md")):
            if path == source:
                continue
            text = path.read_text(encoding="utf-8")
            count = len(pattern.findall(text))
            if count:
                rel = path.relative_to(self.root).as_posix()
                backlink_files.append(rel)
                backlink_occurrences += count
                before[rel] = text

        token_payload = {
            "source": source.relative_to(self.root).as_posix(),
            "target": target.relative_to(self.root).as_posix(),
            "files": {path: _sha256(text) for path, text in sorted(before.items())},
        }
        plan_token = _sha256(
            json.dumps(token_payload, sort_keys=True, separators=(",", ":"))
        )
        return PersonMergePreview(
            source_name=source.stem,
            target_name=target.stem,
            source_path=source.relative_to(self.root).as_posix(),
            target_path=target.relative_to(self.root).as_posix(),
            source_hash=source_hash,
            target_hash=target_hash,
            plan_token=plan_token,
            facts_to_add=facts_to_add,
            duplicate_facts_skipped=duplicate_facts,
            backlink_files=backlink_files,
            backlink_occurrences=backlink_occurrences,
            metadata_conflicts=conflicts,
            _source_text=source_text,
            _target_text=target_text,
            _before=before,
        )

    def apply(
        self, source_name: str, target_name: str, plan_token: str
    ) -> PersonMergeResult:
        try:
            with vault_note_lock(self.root.name):
                preview = self.preview(source_name, target_name)
                if preview.plan_token != plan_token:
                    raise PersonMergeStale(
                        "The vault changed after this merge was previewed. Preview it again."
                    )
                return self.apply_preview_locked(preview)
        except VaultLockTimeout as exc:
            raise PersonMergeError(
                "The vault is busy. Retry the merge shortly."
            ) from exc

    def apply_preview_locked(self, preview: PersonMergePreview) -> PersonMergeResult:
        """Apply a preview while the caller already holds the per-user vault lock."""
        source = self.root / preview.source_path
        target = self.root / preview.target_path
        before = dict(preview._before)
        after: dict[str, Optional[str]] = {}
        changed: list[str] = []
        try:
            merged = self._merge_person_notes(preview)
            merged, _ = _rewrite_links(merged, preview.source_name, preview.target_name)
            _atomic_write(target, merged)
            after[preview.target_path] = merged
            changed.append(preview.target_path)

            for rel in preview.backlink_files:
                if rel == preview.target_path:
                    continue
                rewritten, count = _rewrite_links(
                    before[rel], preview.source_name, preview.target_name
                )
                if count:
                    _atomic_write(self.root / rel, rewritten)
                    after[rel] = rewritten
                    changed.append(rel)

            source.unlink()
            after[preview.source_path] = None
            changed.append(preview.source_path)
        except Exception:
            for rel, content in before.items():
                _atomic_write(self.root / rel, content)
            raise

        return PersonMergeResult(
            action_id=str(uuid.uuid4()),
            preview=preview,
            changed_paths=sorted(set(changed)),
            before=before,
            after=after,
        )

    def _merge_person_notes(self, preview: PersonMergePreview) -> str:
        source_frontmatter, source_body = _split_frontmatter(preview._source_text)
        target_frontmatter, target_body = _split_frontmatter(preview._target_text)

        target_frontmatter["categories"] = _union_values(
            _as_list(target_frontmatter.get("categories")),
            _as_list(source_frontmatter.get("categories")),
        )
        target_frontmatter["aliases"] = _union_values(
            _as_list(target_frontmatter.get("aliases")),
            _as_list(source_frontmatter.get("aliases")),
            [preview.source_name],
        )
        source_distinct = [
            value
            for value in _as_list(source_frontmatter.get("distinct_from"))
            if preview.target_name.casefold() not in _linked_person_names(value)
        ]
        target_frontmatter["distinct_from"] = _union_values(
            _as_list(target_frontmatter.get("distinct_from")), source_distinct
        )
        for key in _SCALAR_IDENTITY_FIELDS:
            if not target_frontmatter.get(key) and source_frontmatter.get(key):
                target_frontmatter[key] = source_frontmatter[key]
        if "updated" in target_frontmatter:
            target_frontmatter["updated"] = date.today().isoformat()

        target_body = _add_media_embeds(target_body, _media_embeds(source_body))
        for heading in _MERGE_SECTIONS:
            existing = {
                _normalise_bullet(line)
                for line in _section_bullets(target_body, heading)
            }
            additions = []
            for bullet in _section_bullets(source_body, heading):
                marker = _normalise_bullet(bullet)
                if marker not in existing:
                    additions.append(bullet)
                    existing.add(marker)
            if additions:
                try:
                    target_body = apply_section_edit(
                        target_body, heading, "\n".join(additions), "append"
                    )
                except SectionEditError as exc:
                    raise PersonMergeError(
                        f"Target person note is missing its {heading} section."
                    ) from exc
        return _join_frontmatter(target_frontmatter, target_body)
