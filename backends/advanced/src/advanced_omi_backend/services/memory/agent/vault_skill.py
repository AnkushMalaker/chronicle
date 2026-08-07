"""Render the vault's conventions as an Agent Skill.

An executor that uses its own native file tools cannot be held to the vault's shape by
a tool boundary, so the shape has to be *taught* instead — and then checked
(``vault_verify``). This module writes that teaching material.

It is generated rather than hand-written for the same reason ``vault_templates`` is the
single source of truth for the scaffold files and the system prompt: three copies of the
schema drift, and a drifted skill is worse than none because it teaches the agent to
produce notes the verifier will reject.

Format follows the repository convention in ``skills/screenshots/SKILL.md`` — YAML
frontmatter with ``name`` and ``description``, then Markdown.
"""

from pathlib import Path

from ..vault_templates import CONVERSATION_TEMPLATE, PERSON_TEMPLATE, TOPIC_TEMPLATE
from ..vault_verify import NEW_NOTE_SCHEMA

SKILL_NAME = "chronicle-vault"


def _required(folder: str) -> str:
    schema = NEW_NOTE_SCHEMA[folder]
    sections = ", ".join(f"`## {name.title()}`" for name in schema["sections"])
    return f"{sections}, and the exact line `{schema['embed']}`"


def render_skill() -> str:
    """The skill document, built from the same constants the vault is scaffolded from."""

    return f"""---
name: {SKILL_NAME}
description: How to record memory into a Chronicle Obsidian vault — note layout, the
  required shape of every note type, and the conventions links depend on. Use whenever
  creating or editing notes under a Chronicle vault root.
---

# Chronicle vault

The vault is the source of truth for memory. Notes are plain Markdown, edited
incrementally: add what is new, never regenerate a note that already exists.

## Layout

- `Conversations/<conversation_id>.md` — one per conversation.
- `People/<Name>.md` — one per person.
- `Topics/<Topic>.md` — one per recurring topic.
- `<Category>/<Name>.md` — any other recurring kind of thing (Places, Projects, Books…),
  each with a hub note and a `Templates/<Category> Template.md`.
- `Templates/` is scaffolding. Never write captured content there.
- `Daily/<YYYY-MM-DD>.md` — one per captured day.

Notes are aggregated by the `categories` property (a wikilink to the category hub, e.g.
`categories: ["[[People]]"]`), **not** by folder. Always set it.

Paths are at most one folder deep: `<Folder>/<Title>.md`. A `/` in a title would mint
nested folders — rephrase the title instead.

## Required shape

A new note must carry its full spine on creation; a note missing it is malformed
permanently, because later edits only append.

- `People/<Name>.md` — {_required("People")}
- `Topics/<Topic>.md` — {_required("Topics")}

Copy the `![[Conversations.base#…]]` embed **verbatim**. It is what auto-lists that
note's conversations; an altered or missing embed silently empties the view.

Each `## Section` appears exactly once per note. Never re-paste a template or a whole
section into a note that already has it.

### Conversation note

```
{CONVERSATION_TEMPLATE}```

### Person note

```
{PERSON_TEMPLATE}```

### Topic note

```
{TOPIC_TEMPLATE}```

## Conventions

- Link profusely — every person, topic and thing is a `[[wikilink]]`. An unresolved
  link is fine; it is a breadcrumb.
- Property and category names are plural and reused across categories (`org`, `role`,
  `date`, `location`, `topics`…). Prefer an existing one over a near-duplicate.
- Use list properties (`["[[A]]", "[[B]]"]`) for anything that may hold more than one
  value.
- Capture what was actually said. Quote key facts; never invent.
- `Unknown Speaker N` is a diarization placeholder, not a person — never give it a
  note or a wikilink.
- Hermes is Chronicle's assistant, not a person. It is `Topics/Hermes.md`.
- Two notes must never differ only by capitalisation: `People/Alice.md` and
  `People/alice.md` cannot both exist on macOS or Windows, and the vault syncs there.
  Search before creating, and reuse the existing spelling exactly.

## Finish by verifying

Before your final message, call `verify_vault`. It reports only problems *you*
introduced, and tells you how to fix each. Fix them and call it again until it passes.
Your final message is a 1–2 sentence summary of what you changed, with no tool calls.
"""


def write_skill(directory: Path) -> Path:
    """Write the skill into ``directory`` and return the path to hand the CLI."""

    skill_dir = directory / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(render_skill(), encoding="utf-8")
    return path
