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
from pathlib import Path
from typing import Dict, List

from .vault_templates import SPINE_TEMPLATES

logger = logging.getLogger("memory_service.vault.scaffold")

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
    valid_props = [p for p in properties if _PROP_LINE.match(p)]
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
    root.mkdir(parents=True, exist_ok=True)
    created: List[str] = []
    for rel, content in files.items():
        fp = root / rel
        if fp.exists():
            continue
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            created.append(rel)
        except Exception as e:  # noqa: BLE001 - scaffold is best-effort, never fatal
            logger.warning("Failed to seed scaffold file %s: %s", fp, e)
    if created:
        logger.info("Seeded vault scaffold in %s: %s", root, ", ".join(created))
    return created
