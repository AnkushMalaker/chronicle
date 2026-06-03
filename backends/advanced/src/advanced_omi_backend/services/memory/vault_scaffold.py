"""Kepano-style aggregation scaffold for a user's vault.

Writes the Obsidian ``.base`` files and thin hub notes that turn a flat collection of
``Conversations/`` / ``People/`` / ``Topics/`` notes into browsable, auto-updating views:

- ``People.base``        — every note categorised under ``[[People]]``.
- ``Conversations.base`` — All / Person / Topic views. The ``Person`` view filters
  ``list(people).contains(this)``; embedded as ``![[Conversations.base#Person]]`` in a
  person note it lists exactly that person's conversations (Bases ``this`` = the host note).
- ``Topics.base``        — every note categorised under ``[[Topics]]``.
- Hubs ``People.md`` / ``Conversations.md`` / ``Topics.md`` — embed the matching base and
  are the link targets for ``categories: ["[[People]]"]`` etc., so wikilinks resolve.

Syntax is copied from the reference Kepano vault (``People.base`` / ``Meetings.base``):
``categories.contains(link("X"))`` for the category filter and ``list(people).contains(this)``
for the per-host view — both verified against that vault.

Seeding is **idempotent**: only missing files are written, so a user's own edits to a base
or hub are never clobbered.
"""

import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("memory_service.vault.scaffold")

# Root-level index notes the scaffold owns. Memory enumeration skips these so they don't
# show up as "memories" or inflate counts (they are views, not captured content).
SCAFFOLD_NOTE_NAMES = frozenset({"People.md", "Conversations.md", "Topics.md"})

_PEOPLE_BASE = """\
filters:
  and:
    - '!file.name.contains("Template")'
    - categories.contains(link("People"))
properties:
  file.name:
    displayName: Name
  note.updated:
    displayName: Updated
views:
  - type: table
    name: All people
    order:
      - file.name
      - updated
    sort:
      - property: file.name
        direction: ASC
"""

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

_TOPICS_BASE = """\
filters:
  and:
    - '!file.name.contains("Template")'
    - categories.contains(link("Topics"))
properties:
  file.name:
    displayName: Topic
views:
  - type: table
    name: All topics
    order:
      - file.name
    sort:
      - property: file.name
        direction: ASC
"""


def _hub(base_name: str, blurb: str) -> str:
    """A thin index note: it's the link target for its category and embeds its base."""
    return f"---\ntags:\n  - categories\n---\n# {base_name}\n\n{blurb}\n\n![[{base_name}.base]]\n"


# filename -> contents. Bases ship at the vault root alongside the hub notes.
_SCAFFOLD: Dict[str, str] = {
    "People.base": _PEOPLE_BASE,
    "Conversations.base": _CONVERSATIONS_BASE,
    "Topics.base": _TOPICS_BASE,
    "People.md": _hub("People", "Everyone mentioned across your conversations."),
    "Conversations.md": _hub("Conversations", "Every conversation, newest first."),
    "Topics.md": _hub("Topics", "Recurring topics across your conversations."),
}


def seed_vault_scaffold(vault_root: Path) -> List[str]:
    """Write any missing ``.base``/hub files into ``vault_root``. Returns files created.

    Idempotent and cheap (a stat per file); safe to call on every write.
    """
    root = Path(vault_root)
    root.mkdir(parents=True, exist_ok=True)
    created: List[str] = []
    for name, content in _SCAFFOLD.items():
        fp = root / name
        if fp.exists():
            continue
        try:
            fp.write_text(content, encoding="utf-8")
            created.append(name)
        except Exception as e:  # noqa: BLE001 - scaffold is best-effort, never fatal
            logger.warning("Failed to seed scaffold file %s: %s", fp, e)
    if created:
        logger.info("Seeded vault scaffold in %s: %s", root, ", ".join(created))
    return created
