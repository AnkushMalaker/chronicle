"""Chronicle memory agent.

A small tool-calling agent that maintains a per-user Obsidian-style markdown vault. Given
one transcribed conversation it decides — using the vault tools — what to write and what
to edit: it creates the conversation note and *surgically edits* existing person/topic
notes rather than regenerating whole documents (the "don't write the whole thing again"
goal). It links people as ``[[wikilinks]]`` so Obsidian Bases can aggregate them.

Runtime: reuses Chronicle's existing provider-agnostic tool-calling primitive
(:func:`async_chat_with_tools`, backed by ``model_registry``) — no new agent framework.
The loop mirrors the one already in ``chat_service`` tool mode.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from advanced_omi_backend.llm_client import async_chat_with_tools

from ..vault_templates import CONVERSATION_TEMPLATE, PERSON_TEMPLATE, TOPIC_TEMPLATE
from .vault_tools import (
    VAULT_SEARCH_TOOL_SCHEMAS,
    VAULT_TOOL_SCHEMAS,
    VaultToolError,
    VaultTools,
)

logger = logging.getLogger("memory_service.agent")

MAX_TOOL_ROUNDS = 16
MAX_SEARCH_ROUNDS = 6
# Bail out after this many consecutive rounds that raised a tool error but landed no
# new note edit. That pattern is the model retrying a failing edit (e.g. a stale
# edit_note anchor) and is almost never productive — aborting saves the rest of the
# MAX_TOOL_ROUNDS budget (each round is a full LLM call). Belt-and-suspenders: with
# memory jobs serialised on one worker, the concurrent-write cause can no longer occur.
MAX_STALLED_ROUNDS = 3

SEARCH_SYSTEM_PROMPT = """\
You are a retrieval agent over a personal Obsidian-style markdown VAULT. Given a user's
question, find the notes that answer it and return their relevant content.

# Vault layout
- People/<Name>.md — one per person (## About + dated ## Mentions).
- Conversations/<id>.md — frontmatter people:[[..]] topics:[[..]]; sections Summary /
  Key Facts / Action Items.
- Topics/<Topic>.md — one per topic.
- <Category>/<Name>.md — other kinds of things (Places, Projects, Books…); notes carry a
  `categories: ["[[<Category>]]"]` property. (Ignore the Templates/ folder — it's scaffolding.)

# How to search (you have read-only tools: grep, glob, read_note)
1. Turn the question into one or more `grep` regex patterns over note CONTENTS. Names,
   places, and facts appear verbatim, so search for the salient keyword(s) — e.g. for
   "what 3D printer does Partham use?" grep `3D|printer|Dosink`. Use alternation `a|b`
   and character classes `[Hh]` rather than one long literal phrase, which rarely matches.
2. Use `glob` (e.g. `People/*.md`) to find a person/topic note by name.
3. `read_note` the most relevant notes to confirm and gather context.
4. When you have the answer, STOP calling tools and reply with a concise answer that
   quotes the key facts verbatim and names the notes you used. Do not invent facts.
{{vault_summary}}"""

# System prompt id (registered in prompt_defaults). The constant below is the fallback
# used when the registry is unavailable, and is the source of the registered default.
AGENT_SYSTEM_PROMPT_ID = "memory.agent_system"


# The note templates ARE the schema — embedded from vault_templates (the same strings
# seeded into the vault's Templates/ folder) so the model's notion of a note's shape can
# never drift from the files. The templates' Obsidian `{{date}}`/`{{title}}` tokens are
# rewritten to `<date>`/`<title>` for the prompt so the ONLY mustache placeholder left is
# `{{vault_summary}}` — otherwise a LangFuse `compile()` would blank the others. Built by
# concatenation (not an f-string) so braces and the trailing slot survive intact.
def _for_prompt(template: str) -> str:
    return template.replace("{{date}}", "<date>").replace("{{title}}", "<title>")


DEFAULT_AGENT_SYSTEM_PROMPT = (
    """\
You are Chronicle's memory agent. You maintain a personal Obsidian-style markdown VAULT by
editing files with tools. Given one transcribed conversation, record it and update what the
vault knows about the people, topics, and things involved — making the SMALLEST edits that
capture the new information. Never regenerate a whole note when an edit will do.

# Vault layout
- Conversations/<conversation_id>.md — one per conversation.
- People/<Name>.md — one per person (speakers and named people).
- Topics/<Topic>.md — one per recurring topic.
- <Category>/<Name>.md — notes for any OTHER recurring kind of thing (Places, Projects,
  Books, Companies…). Each category has a hub note <Category>.md and a
  Templates/<Category> Template.md describing its shape.
- Templates/ holds note templates and Templates/Bases/ the aggregation views — this is
  scaffolding; never write captured content there.

Notes are aggregated by the `categories` property (a wikilink to the category hub, e.g.
`categories: ["[[People]]"]`), NOT by folder — so always set `categories` correctly.

# Conventions (this vault follows the Kepano / "file over app" style)
- Link profusely: every person, topic, and thing is a [[wikilink]]. An unresolved link
  (no note yet) is fine — it is a breadcrumb for later.
- Category names and property names are PLURAL where applicable and REUSED across
  categories (org, role, date, location, topics…) so things stay findable. Prefer an
  existing category/property over inventing a near-duplicate.
- Use `list` properties (`["[[A]]", "[[B]]"]`) for anything that may hold more than one value.
- Capture what was actually said; quote key facts verbatim; never invent.

# Note templates — fill these EXACTLY (they are the schema)
Conversation note — `Conversations/<conversation_id>.md`:
```
"""
    + _for_prompt(CONVERSATION_TEMPLATE)
    + """```
Person note — `People/<Name>.md`:
```
"""
    + _for_prompt(PERSON_TEMPLATE)
    + """```
Topic note — `Topics/<Topic>.md`:
```
"""
    + _for_prompt(TOPIC_TEMPLATE)
    + """```
In a template: replace `<date>` with the ISO date and `<title>` with the note's title;
fill the blank properties and bullets. Copy the `![[Conversations.base#…]]` embed line
VERBATIM into every new person/topic/category note — it auto-lists that note's
conversations; never edit or remove it.

# Organic categories
Most conversations only touch People and Topics. But when something is a substantive,
recurring KIND of thing that is not People/Topics/Conversations (a place, project, book,
company…), call `create_category(name, properties)` ONCE — `name` plural (e.g. "Places"),
`properties` the few short reusable keys its notes need (e.g. ["location", "type"]). That
writes its template + base + hub. Then `read_note` `Templates/<Category> Template.md`,
fill it, and `write_note` the note at `<Category>/<Name>.md` with
`categories: ["[[<Category>]]"]`. Do NOT over-create categories — only when the thing will
plausibly recur and matters.

# How to work
1. SEARCH first: `glob` (e.g. `People/*.md`) to see what exists and `grep` (regex over
   contents) to find a person/topic/fact. Reuse exact existing note names so links resolve.
2. `write_note` the conversation note from the Conversation template; put every identified
   person in `people:` and every theme in `topics:` as [[wikilinks]].
3. For each person/topic/thing: if its note exists, READ it then `edit_section` to add
   genuinely new facts — `append` a bullet under `## About` and a dated line under
   `## Mentions`. Otherwise `write_note` it from the matching template. `write_note` is
   for CREATING a note — never use it (and never use overwrite) to "update" an existing
   person/topic note, and never paste the template scaffold (`## About`/`## Conversations`/
   `## Mentions`) into a note that already has it. Each section must appear exactly
   once. Don't duplicate facts already present.
4. `edit_section` targets a note's STRUCTURE, not a slice of its text: pass the section
   heading (e.g. `About`, `Mentions`) or a `^block-ref` as `target`, the new bullet
   line(s) as `text`, and an `operation` of append (default) / prepend / replace. Add
   only the new line(s) — never re-paste the whole section. Use `edit_note` for
   frontmatter and surgical mid-line fixes.
5. Other conversations may be recorded into this vault CONCURRENTLY. `edit_section` does
   not depend on the section's current text, so it keeps working when a note changed
   between your read and your edit — prefer it. Never re-write or re-paste a whole
   section, and never re-add content that is already present.
6. If the conversation re-identifies a speaker (e.g. "Speaker 0" is actually Alice), use
   `rename_person` to fix the name and all its backlinks.
7. Keep going until everything is recorded, then reply with a 1-2 sentence summary of what
   you changed. Do not call tools in that final message.

Be precise and conservative: capture what was actually said, link things, avoid invention.
{{vault_summary}}"""
)


@dataclass
class MemoryAgentResult:
    conversation_id: str
    rounds: int
    touched: List[str]
    summary: str
    tool_calls: int = 0
    errors: List[str] = field(default_factory=list)
    truncated: bool = (
        False  # loop ended on a truncated/empty LLM response, not a deliberate finish
    )
    stalled: bool = (
        False  # loop aborted after repeated no-progress error rounds (stuck retrying)
    )


async def _get_prompt(prompt_id: str, default: str, vault_summary: str = "") -> str:
    """Fetch a (user-overridable) prompt from the registry, else fall back to ``default``.

    Both prompts carry a ``{{vault_summary}}`` slot for learned per-user conventions.
    """
    try:
        from advanced_omi_backend.prompt_registry import get_prompt_registry

        registry = get_prompt_registry()
        return await registry.get_prompt(prompt_id, vault_summary=vault_summary)
    except Exception as e:  # noqa: BLE001 - registry optional; fall back to constant
        logger.debug(
            "prompt registry unavailable (%s); using default for %s", e, prompt_id
        )
        return default.replace("{{vault_summary}}", vault_summary)


class MemoryAgent:
    """Runs the tool loop that turns a transcript into vault edits."""

    def __init__(self, vault_root: Path, operation: str = "memory_agent"):
        # `operation` selects the model/params from model_registry. A dedicated
        # "memory_agent" operation is used (not "memory_extraction", which may force
        # response_format=json and conflict with tool calling): reasoning models spend
        # completion tokens on reasoning before emitting any tool call, so this
        # operation carries a larger max_tokens budget and a low reasoning_effort.
        self.tools = VaultTools(vault_root)
        self.operation = operation

    async def run(
        self,
        transcript: str,
        conversation_id: str,
        *,
        date: Optional[str] = None,
        duration_minutes: Optional[float] = None,
        vault_summary: str = "",
        guidance: str = "",
    ) -> MemoryAgentResult:
        date = date or datetime.now(timezone.utc).isoformat()
        system_prompt = await _get_prompt(
            AGENT_SYSTEM_PROMPT_ID, DEFAULT_AGENT_SYSTEM_PROMPT, vault_summary
        )

        guidance_block = f"\n\n{guidance}" if guidance else ""
        task = (
            f"New conversation to record.\n"
            f"conversation_id: {conversation_id}\n"
            f"date: {date}\n"
            f"duration_minutes: {duration_minutes if duration_minutes is not None else 'unknown'}\n\n"
            f"Transcript (speaker-labelled):\n{transcript}"
            f"{guidance_block}"
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        tool_calls = 0
        errors: List[str] = []
        truncation_retried = False
        stalled_rounds = 0  # consecutive rounds that erred but landed no new edit

        for round_idx in range(MAX_TOOL_ROUNDS):
            response = await async_chat_with_tools(
                messages, tools=VAULT_TOOL_SCHEMAS, operation=self.operation
            )
            choice = response.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                summary = (msg.content or "").strip()
                if not summary or choice.finish_reason == "length":
                    # Reasoning models can burn the whole completion budget on
                    # reasoning and return an empty/truncated message with no tool
                    # calls (finish_reason="length") — that is NOT a deliberate
                    # completion. Retry the round once, then abort as truncated.
                    if not truncation_retried:
                        truncation_retried = True
                        logger.warning(
                            "memory agent got truncated/empty response for conv=%s "
                            "(finish_reason=%s) — retrying round %d",
                            conversation_id,
                            choice.finish_reason,
                            round_idx + 1,
                        )
                        continue
                    logger.error(
                        "memory agent aborted on truncated/empty response for conv=%s "
                        "(finish_reason=%s, rounds=%d, tools=%d, touched=%d)",
                        conversation_id,
                        choice.finish_reason,
                        round_idx + 1,
                        tool_calls,
                        len(self.tools.touched),
                    )
                    return MemoryAgentResult(
                        conversation_id=conversation_id,
                        rounds=round_idx + 1,
                        touched=sorted(self.tools.touched),
                        summary=summary,
                        tool_calls=tool_calls,
                        errors=errors,
                        truncated=True,
                    )
                logger.info(
                    "memory agent done: conv=%s rounds=%d tools=%d touched=%d",
                    conversation_id,
                    round_idx + 1,
                    tool_calls,
                    len(self.tools.touched),
                )
                return MemoryAgentResult(
                    conversation_id=conversation_id,
                    rounds=round_idx + 1,
                    touched=sorted(self.tools.touched),
                    summary=summary,
                    tool_calls=tool_calls,
                    errors=errors,
                )

            messages.append(msg.model_dump())
            touched_before = len(self.tools.touched)
            errors_before = len(errors)
            for tc in msg.tool_calls:
                tool_calls += 1
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = self.tools.dispatch(name, args)
                except VaultToolError as e:
                    result = f"Error: {e}"  # surfaced to model so it can self-correct
                    errors.append(f"{name}: {e}")
                except Exception as e:  # noqa: BLE001 - unexpected tool failure
                    result = f"Error: {type(e).__name__}: {e}"
                    errors.append(f"{name}: {e}")
                    logger.exception("memory agent tool %s crashed", name)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )

            # No-progress guard: a round that raised a tool error but edited no note is
            # the model retrying a failing edit. Count consecutive such rounds and bail
            # before exhausting the round budget on a doomed retry loop. A round that
            # landed any edit (touched grew) resets the counter.
            made_edit = len(self.tools.touched) > touched_before
            had_error = len(errors) > errors_before
            if had_error and not made_edit:
                stalled_rounds += 1
                if stalled_rounds >= MAX_STALLED_ROUNDS:
                    logger.warning(
                        "memory agent stalled for conv=%s: %d consecutive no-progress "
                        "error rounds (rounds=%d, tools=%d, touched=%d) — aborting",
                        conversation_id,
                        stalled_rounds,
                        round_idx + 1,
                        tool_calls,
                        len(self.tools.touched),
                    )
                    return MemoryAgentResult(
                        conversation_id=conversation_id,
                        rounds=round_idx + 1,
                        touched=sorted(self.tools.touched),
                        summary="(stopped: stalled retrying a failing edit)",
                        tool_calls=tool_calls,
                        errors=errors,
                        stalled=True,
                    )
            else:
                stalled_rounds = 0

        logger.warning(
            "memory agent hit MAX_TOOL_ROUNDS for conv=%s (touched=%d)",
            conversation_id,
            len(self.tools.touched),
        )
        return MemoryAgentResult(
            conversation_id=conversation_id,
            rounds=MAX_TOOL_ROUNDS,
            touched=sorted(self.tools.touched),
            summary="(stopped at max rounds)",
            tool_calls=tool_calls,
            errors=errors,
        )


@dataclass
class VaultSearchResult:
    answer: str
    notes: List[
        Dict[str, str]
    ]  # [{"path": ..., "content": ...}] for notes the agent read
    rounds: int = 0


async def search_vault(
    query: str,
    vault_root: Path,
    *,
    operation: str = "memory_agent",
    max_rounds: int = MAX_SEARCH_ROUNDS,
    vault_summary: str = "",
) -> VaultSearchResult:
    """Read-only retrieval agent: the model drives grep/glob/read to answer ``query``.

    Models Claude Code's search — the LLM formulates ripgrep patterns; there is no query
    preprocessing. Returns the synthesised answer plus the notes the agent read (which it
    chose to read because they were relevant), for use as memory context.
    """
    tools = VaultTools(vault_root)
    system_prompt = await _get_prompt(
        "memory.search_system", SEARCH_SYSTEM_PROMPT, vault_summary
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]
    read_notes: Dict[str, str] = {}  # path -> content, in read order

    for round_idx in range(max_rounds):
        response = await async_chat_with_tools(
            messages, tools=VAULT_SEARCH_TOOL_SCHEMAS, operation=operation
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return VaultSearchResult(
                answer=(msg.content or "").strip(),
                notes=[{"path": p, "content": c} for p, c in read_notes.items()],
                rounds=round_idx + 1,
            )
        messages.append(msg.model_dump())
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = tools.dispatch(name, args)
                if name == "read_note" and not result.startswith("Error:"):
                    read_notes[args.get("path", "?")] = result
            except VaultToolError as e:
                result = f"Error: {e}"
            except Exception as e:  # noqa: BLE001
                result = f"Error: {type(e).__name__}: {e}"
                logger.exception("vault search tool %s crashed", name)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return VaultSearchResult(
        answer="(search stopped at max rounds)",
        notes=[{"path": p, "content": c} for p, c in read_notes.items()],
        rounds=max_rounds,
    )
