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

from .vault_tools import (
    VAULT_SEARCH_TOOL_SCHEMAS,
    VAULT_TOOL_SCHEMAS,
    VaultToolError,
    VaultTools,
)

logger = logging.getLogger("memory_service.agent")

MAX_TOOL_ROUNDS = 16
MAX_SEARCH_ROUNDS = 6

SEARCH_SYSTEM_PROMPT = """\
You are a retrieval agent over a personal Obsidian-style markdown VAULT. Given a user's
question, find the notes that answer it and return their relevant content.

# Vault layout
- People/<Name>.md — one per person (## About + dated ## Mentions).
- Conversations/<id>.md — frontmatter people:[[..]] topics:[[..]]; sections Summary /
  Key Facts / People / Action Items.
- Topics/<Topic>.md — one per topic.

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

DEFAULT_AGENT_SYSTEM_PROMPT = """\
You are Chronicle's memory agent. You maintain a personal Obsidian-style markdown VAULT by
editing files with tools. Your job: given one transcribed conversation, record it and update
what the vault knows about the people and topics involved — making the SMALLEST edits that
capture the new information. Never regenerate a whole note when an edit will do.

# Vault layout
- Conversations/<conversation_id>.md — one per conversation.
- People/<Name>.md — one per person (speakers and named people).
- Topics/<Topic>.md — one per recurring topic (optional; create only for substantive themes).

# Note formats
Conversation note:
---
categories: ["[[Conversations]]"]
conversation_id: <id>
date: <iso8601>
people: ["[[Name]]", ...]      # wikilinks — one per identified/ named person
topics: ["[[Topic]]", ...]
duration_minutes: <n>
---
## <Title — 3-8 words>
### Summary
<2-3 sentences>
### Key Facts
- <verbatim WH-details: WHO/WHAT/WHERE/WHEN/HOW MUCH — never paraphrase names, titles, places, dates, numbers>
### Action Items
- [ ] <task>

Person note:
---
categories: ["[[People]]"]
aliases: []
created: <iso8601>
updated: <iso8601>
---
## About
- <stable facts: role, org, relationships, preferences>
## Conversations
![[Conversations.base#Person]]
## Mentions
- <date> — <what was learned in this conversation> ([[<conversation title or id>]])

(The `![[Conversations.base#Person]]` line is a literal Obsidian Base embed — copy it
verbatim into every NEW person note; it auto-lists that person's conversations. Never
edit or remove it.)

# How to work
1. First SEARCH the vault to see which people and topics already have notes — use `glob`
   (e.g. `People/*.md`) to list them and `grep` (regex over contents) to check for a person
   or fact. Reuse exact existing note names so links resolve.
2. Create the conversation note with write_note. Put every identified person in `people:` as
   a [[wikilink]].
3. For each person: if a People/<Name>.md exists, READ it then EDIT it (edit_note) to append
   genuinely new facts under ## About and a dated line under ## Mentions. If it does not
   exist, write_note a new one. Do not duplicate facts already present.
4. edit_note requires old_text to match the file EXACTLY and UNIQUELY — include enough
   surrounding context (e.g. the section header line). Edit frontmatter as text too.
5. If the conversation re-identifies a speaker (e.g. "Speaker 0" is actually Alice), use
   rename_person to fix the name and all its backlinks.
6. Keep going until everything is recorded, then reply with a 1-2 sentence summary of what
   you changed. Do not call tools in that final message.

Be precise and conservative: capture what was actually said, link people, avoid invention.
{{vault_summary}}"""


@dataclass
class MemoryAgentResult:
    conversation_id: str
    rounds: int
    touched: List[str]
    summary: str
    tool_calls: int = 0
    errors: List[str] = field(default_factory=list)


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

    def __init__(self, vault_root: Path, operation: str = "chat"):
        # `operation` selects the model/params from model_registry. "chat" is used (not
        # "memory_extraction") because the latter may force response_format=json, which
        # conflicts with tool calling.
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

        for round_idx in range(MAX_TOOL_ROUNDS):
            response = await async_chat_with_tools(
                messages, tools=VAULT_TOOL_SCHEMAS, operation=self.operation
            )
            choice = response.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                summary = (msg.content or "").strip()
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
    operation: str = "chat",
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
