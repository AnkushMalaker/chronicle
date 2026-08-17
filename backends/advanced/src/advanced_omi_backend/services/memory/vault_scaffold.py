"""Kepano-style scaffold for a user's vault.

Seeds the **spine** that turns a flat collection of notes into a browsable,
auto-updating Obsidian vault, laid out exactly like the reference Kepano vault
(``untracked/kepano``): note templates live in ``Templates/``, Obsidian ``.base``
aggregation files in ``Templates/Bases/``, and thin **hub notes** at the vault root.

```
vault_root/
  People.md  Conversations.md  Topics.md          ← category hub notes (root)
  Templates/
    Person Template.md  Conversation Template.md  Topic Template.md
    Bases/
      People.base  Conversations.base  Topics.base
```

- **Templates** (``vault_templates.SPINE_TEMPLATES``) are the schema the memory agent
  fills; the ``!file.name.contains("Template")`` filter in every base keeps them out of
  the aggregated views.
- **Bases** aggregate by the ``categories`` property — ``categories.contains(link("X"))``
  — not by folder, so the physical ``Conversations/``/``People/`` folders are just tidiness.
  ``Conversations.base`` has a ``Person`` view filtering ``list(people).contains(this)``;
  embedded as ``![[Conversations.base#Person]]`` in a person note it lists exactly that
  person's conversations (Bases ``this`` = the host note).
- **Hubs** are the link targets for ``categories: ["[[People]]"]`` etc. (so wikilinks
  resolve) and embed the matching base. Obsidian resolves ``.base`` embeds by basename, so
  ``![[People.base]]`` works even though the base lives in ``Templates/Bases/``.

Seeding is **idempotent**: only missing files are written, so a user's own edits to a
template, base, or hub are never clobbered.

New *categories* (Places, Projects, Books…) are not seeded here — they grow organically
via :func:`build_category_files`, which stamps a template + base + hub for a brand-new
category the first time the agent needs one.
"""

import logging
import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, List

from .vault_templates import SPINE_TEMPLATES

logger = logging.getLogger("memory_service.vault.scaffold")


class VaultPathError(ValueError):
    """A vault path would escape its root or traverse a symbolic link."""


def safe_vault_relative_path(path: str | Path) -> str:
    """Return a portable vault-relative path or reject unsafe path syntax.

    This is intentionally less restrictive than the memory agent's note-path
    contract: scaffold files legitimately live three levels deep under
    ``Templates/Bases``. It only establishes the common security boundary: no
    absolute/drive-qualified paths, traversal, empty components, backslashes, or
    control characters. Unicode and ordinary spaces remain valid.
    """
    if not isinstance(path, (str, Path)):
        raise VaultPathError("Vault paths must be strings or Path objects.")
    raw = str(path)
    if not raw:
        raise VaultPathError("Vault paths must not be empty.")
    if "\\" in raw:
        raise VaultPathError("Vault paths must use '/' separators.")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise VaultPathError("Vault paths must not contain control characters.")

    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    raw_parts = raw.split("/")
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise VaultPathError(f"Unsafe vault-relative path: {raw!r}.")
    return posix.as_posix()


def confined_vault_path(vault_root: Path, relative_path: str | Path) -> Path:
    """Resolve a lexical child path without allowing symlink traversal.

    The returned path remains lexical (rather than the resolved target) so callers
    preserve the user's filename casing. Every existing component, including a
    broken leaf link, is checked before the final resolved-boundary assertion.
    """
    root = Path(vault_root).absolute()
    if root.is_symlink():
        raise VaultPathError(f"Vault root must not be a symbolic link: {root}.")
    if not root.is_dir():
        raise VaultPathError(f"Vault root is not a directory: {root}.")

    rel = Path(safe_vault_relative_path(relative_path))
    resolved_root = root.resolve(strict=True)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise VaultPathError(
                f"Vault path must not traverse a symbolic link: {rel.as_posix()!r}."
            )

    resolved_target = (root / rel).resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_root):
        raise VaultPathError(f"Vault path escapes its root: {rel.as_posix()!r}.")
    return root / rel


def validate_category_name(category: str) -> str:
    """Normalize a safe category title for paths, YAML strings, and wikilinks."""
    if not isinstance(category, str):
        raise VaultPathError("Category names must be strings.")
    cleaned = unicodedata.normalize("NFC", category.strip())
    if not cleaned:
        raise VaultPathError("Category names must not be empty.")
    safe = safe_vault_relative_path(cleaned)
    if len(PurePosixPath(safe).parts) != 1:
        raise VaultPathError(
            "Category names must be one plain title without path separators."
        )
    if not any(character.isalnum() for character in safe) or any(
        not (character.isalnum() or character in " _-") for character in safe
    ):
        raise VaultPathError(
            "Category names may contain only Unicode letters/numbers, spaces, "
            "hyphens, and underscores."
        )
    return safe


# Folder (relative to the vault root) that holds templates and bases. Notes under it are
# scaffolding, never captured content — enumeration skips the whole subtree.
TEMPLATES_DIR = "Templates"
BASES_DIR = f"{TEMPLATES_DIR}/Bases"

# Root-level hub notes the scaffold owns (and any organically-created ``<Category>.md``).
# Memory enumeration skips these so they don't show up as "memories" or inflate counts.
SCAFFOLD_NOTE_NAMES = frozenset({"People.md", "Conversations.md", "Topics.md"})


def _base(category: str, *, extra_views: str = "") -> str:
    """A minimal ``.base``: everything categorised under ``[[category]]`` (templates excluded)."""
    return (
        "filters:\n"
        "  and:\n"
        "    - '!file.name.contains(\"Template\")'\n"
        f'    - categories.contains(link("{category}"))\n'
        "properties:\n"
        "  file.name:\n"
        f"    displayName: {category}\n"
        "  note.updated:\n"
        "    displayName: Updated\n"
        "views:\n"
        "  - type: table\n"
        f"    name: All {category.lower()}\n"
        "    order:\n"
        "      - file.name\n"
        "      - updated\n"
        "    sort:\n"
        "      - property: file.name\n"
        "        direction: ASC\n"
        f"{extra_views}"
    )


_CONVERSATIONS_BASE = """\
filters:
  and:
    - '!file.name.contains("Template")'
    - categories.contains(link("Conversations"))
properties:
  note.date:
    displayName: Date
  note.people:
    displayName: People
  note.topics:
    displayName: Topics
  file.name:
    displayName: Conversation
views:
  - type: table
    name: All
    order:
      - file.name
      - date
      - people
      - topics
    sort:
      - property: date
        direction: DESC
  - type: table
    name: Person
    filters:
      and:
        - list(people).contains(this)
    order:
      - file.name
      - date
      - topics
    sort:
      - property: date
        direction: DESC
  - type: table
    name: Topic
    filters:
      and:
        - list(topics).contains(this)
    order:
      - file.name
      - date
      - people
    sort:
      - property: date
        direction: DESC
"""


def _hub(category: str, blurb: str) -> str:
    """A thin index note: the link target for its category that embeds its base."""
    return f"---\ntags:\n  - categories\n---\n# {category}\n\n{blurb}\n\n![[{category}.base]]\n"


# vault-relative path -> contents. Templates/bases live under Templates/; hubs at root.
def _build_scaffold() -> Dict[str, str]:
    scaffold: Dict[str, str] = {}
    # Spine templates (the agent's fill-in schema).
    for name, content in SPINE_TEMPLATES.items():
        scaffold[f"{TEMPLATES_DIR}/{name}"] = content
    # Bases (aggregation layer).
    scaffold[f"{BASES_DIR}/People.base"] = _base("People")
    scaffold[f"{BASES_DIR}/Conversations.base"] = _CONVERSATIONS_BASE
    scaffold[f"{BASES_DIR}/Topics.base"] = _base("Topics")
    # Hub notes (root).
    scaffold["People.md"] = _hub(
        "People", "Everyone mentioned across your conversations."
    )
    scaffold["Conversations.md"] = _hub(
        "Conversations", "Every conversation, newest first."
    )
    scaffold["Topics.md"] = _hub(
        "Topics", "Recurring topics across your conversations."
    )
    return scaffold


_SCAFFOLD: Dict[str, str] = _build_scaffold()


def canonical_vault_scaffold() -> Dict[str, str]:
    """Return a copy of the exact built-in scaffold used to seed a new vault."""

    return dict(_SCAFFOLD)


def is_scaffold_note(path: Path, vault_root: Path) -> bool:
    """True if ``path`` is scaffolding (a root hub note, or anything under ``Templates/``).

    Used by note enumeration to keep templates/bases/hubs from being counted or returned
    as captured memories.
    """
    try:
        rel = path.relative_to(vault_root)
    except ValueError:
        return False
    if rel.parts and rel.parts[0] == TEMPLATES_DIR:
        return True
    return path.name in SCAFFOLD_NOTE_NAMES


def seed_vault_scaffold(vault_root: Path) -> List[str]:
    """Write any missing spine template/base/hub files into ``vault_root``.

    Returns the vault-relative paths created. Idempotent and cheap (a stat per file);
    safe to call on every write.
    """
    return _write_files(Path(vault_root), _SCAFFOLD)


# --- organic category creation ----------------------------------------------

_PROP_LINE = re.compile(r"^[a-z][a-z0-9_]*$")


def build_category_files(category: str, properties: List[str]) -> Dict[str, str]:
    """Build the template/base/hub files for a brand-new organic ``category``.

    ``category`` is the (pluralised) category name, e.g. ``"Places"``; ``properties`` are
    the short, reusable frontmatter keys the category's notes should carry (e.g.
    ``["location", "type"]``). Returns ``{vault-relative path: contents}`` for the three
    files — a ``Templates/<Category> Template.md``, ``Templates/Bases/<Category>.base`` and
    a root ``<Category>.md`` hub — mirroring the spine layout so the new category behaves
    exactly like People/Conversations/Topics.
    """
    category = validate_category_name(category)
    valid_props = [
        prop for prop in properties if isinstance(prop, str) and _PROP_LINE.match(prop)
    ]
    prop_block = "".join(f"{p}:\n" for p in valid_props)
    template = (
        "---\n"
        f'categories:\n  - "[[{category}]]"\n'
        f"{prop_block}"
        "created: {{date}}\n"
        "updated: {{date}}\n"
        "---\n"
        "## About\n-\n"
    )
    return {
        f"{TEMPLATES_DIR}/{category} Template.md": template,
        f"{BASES_DIR}/{category}.base": _base(category),
        f"{category}.md": _hub(category, f"Everything categorised under {category}."),
    }


def write_category(vault_root: Path, category: str, properties: List[str]) -> List[str]:
    """Idempotently seed a new category's template/base/hub. Returns paths created."""
    return _write_files(Path(vault_root), build_category_files(category, properties))


def _write_files(root: Path, files: Dict[str, str]) -> List[str]:
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    # Preflight the complete set before writing any file. Organic category creation
    # must be all-or-nothing with respect to path safety; otherwise a malicious name
    # could create one file outside the vault before a later path is rejected.
    targets = [
        (safe_vault_relative_path(rel), confined_vault_path(root, rel), content)
        for rel, content in files.items()
    ]
    created: List[str] = []
    for rel, fp, content in targets:
        if fp.exists():
            continue
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after mkdir so an existing/broken link in a newly reached
            # component is never followed by write_text().
            fp = confined_vault_path(root, rel)
            fp.write_text(content, encoding="utf-8")
            created.append(rel)
        except VaultPathError:
            raise
        except Exception as e:  # noqa: BLE001 - scaffold is best-effort, never fatal
            logger.warning("Failed to seed scaffold file %s: %s", fp, e)
    if created:
        logger.info("Seeded vault scaffold in %s: %s", root, ", ".join(created))
    return created
