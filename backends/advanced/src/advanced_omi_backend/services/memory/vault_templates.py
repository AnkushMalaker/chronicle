"""Canonical note templates for a Chronicle vault — the single source of truth.

Each template is the *schema contract* for a note type: a fixed-ish set of frontmatter
properties + sections that the memory agent fills in, so every note of a kind has the
same shape instead of the LLM improvising fields each time. The same strings are used
two ways, which is why they live here once:

1. **Seeded as files** into ``{vault}/Templates/`` by :mod:`vault_scaffold` — so a human
   opening the vault in Obsidian gets real, reusable templates (and ``notesmd-cli create
   --template`` / the Obsidian Templates plugin can use them directly).
2. **Injected into the memory-agent system prompt** (:mod:`agent.memory_agent`) as the
   exact shape to fill — so the model's notion of structure can never drift from the file.

Conventions follow the Kepano / Steph Ango vault (``untracked/kepano/stephango-vault.md``):
``categories`` is a wikilink list to the category hub note; properties are short and
reusable across categories (``org``, ``role``, ``date``, ``topics``…); ``list`` types are
used wherever more than one value is plausible; ``{{date}}``/``{{title}}`` are Obsidian
core template variables (resolved by Obsidian, by ``notesmd-cli --template``, or written
literally by the agent).

These three are the **spine** — always present. New categories (Places, Projects, Books…)
grow their own templates organically via the meta-template in :mod:`vault_scaffold`.
"""

# --- spine note templates ---------------------------------------------------

CONVERSATION_TEMPLATE = """\
---
categories:
  - "[[Conversations]]"
conversation_id:
date: {{date}}
people: []
topics: []
duration_minutes:
---
## {{title}}

### Summary


### Key Facts
-

### Action Items
- [ ]
"""

PERSON_TEMPLATE = """\
---
categories:
  - "[[People]]"
aliases: []
org:
role:
relationship:
location:
created: {{date}}
updated: {{date}}
---
## About
-

## Conversations
![[Conversations.base#Person]]

## Mentions
- {{date}} —
"""

TOPIC_TEMPLATE = """\
---
categories:
  - "[[Topics]]"
created: {{date}}
updated: {{date}}
---
## About
-

## Conversations
![[Conversations.base#Topic]]
"""

# template filename (in Templates/) -> contents
SPINE_TEMPLATES: dict[str, str] = {
    "Conversation Template.md": CONVERSATION_TEMPLATE,
    "Person Template.md": PERSON_TEMPLATE,
    "Topic Template.md": TOPIC_TEMPLATE,
}
