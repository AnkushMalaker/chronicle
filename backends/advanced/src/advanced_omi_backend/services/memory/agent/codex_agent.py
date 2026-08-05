"""Codex CLI memory-agent executor.

Alternative executor for the Chronicle memory agent: instead of the built-in
tool-calling loop (metered per-call API usage via the model registry), it shells out
to the OpenAI Codex CLI (``codex exec``) working directly inside the user's vault
directory — so vault recording runs on a ChatGPT subscription (``~/.codex/auth.json``,
mounted as ``CODEX_HOME`` in containers) instead of API calls.

Selected via config.yml ``memory.agent_executor: codex``. Satisfies the same contract
as :class:`MemoryAgent` (constructor + ``run() -> MemoryAgentResult``) so the
chronicle provider's note-guarantee retry, audit recording, and job bookkeeping work
unchanged. Differences from the direct loop:

- ``touched``/``removed`` are computed from a before/after filesystem diff, never
  trusted from the CLI's own reporting. A file rename shows up as a removal (with its
  pre-removal content preserved for the ledger) plus a creation — the pair is not
  re-associated.
- The per-write ``vault_note_lock`` backstops don't apply (Codex edits files itself),
  so the whole run holds the run-scale :func:`vault_run_lock` on the same key, and
  the hard rules those tools enforced are stated in the prompt instead.
- ``force_fallback=True`` (the note-guarantee recovery attempt) delegates to the
  direct :class:`MemoryAgent` — if a Codex run failed to produce the note, retrying
  through a different path beats re-running the same CLI.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..vault_templates import CONVERSATION_TEMPLATE, PERSON_TEMPLATE, TOPIC_TEMPLATE
from . import codex_quota
from .memory_agent import MemoryAgentResult, _for_prompt, _get_prompt

logger = logging.getLogger("memory_service.agent.codex")

CODEX_AGENT_SYSTEM_PROMPT_ID = "memory.codex_agent_system"

# Fallback timeout when config carries none; the run lock TTL is derived from it.
DEFAULT_RUN_TIMEOUT_SECONDS = 900
_STDERR_TAIL_CHARS = 2000

DEFAULT_CODEX_AGENT_SYSTEM_PROMPT = (
    """\
You are Chronicle's memory agent. You maintain a personal Obsidian-style markdown VAULT —
the current working directory — by reading and editing its files directly. Given one
transcribed conversation, record it and update what the vault knows about the people,
topics, and things involved — making the SMALLEST edits that capture the new information.
Never regenerate a whole note when an edit will do.

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
company…), mint the category ONCE by hand: create `Templates/<Category> Template.md`
(model it on `Templates/Topic Template.md`, with the few short reusable frontmatter keys
its notes need), a hub note `<Category>.md` (model it on an existing hub), and — if
`Templates/Bases/` holds per-category `.base` files — a matching one copied from an
existing category's with the names substituted. Then file notes at `<Category>/<Name>.md`
with `categories: ["[[<Category>]]"]`. Do NOT over-create categories — only when the
thing will plausibly recur and matters.

# Required outcome
Inspect the vault with the tools and reading strategy you judge appropriate before editing.
Reuse exact existing note names so [[wikilinks]] resolve. Then:
1. Create the conversation note at `Conversations/<conversation_id>.md` from the
   Conversation template; put every identified person in `people:` and every theme in
   `topics:` as [[wikilinks]].
2. For each person/topic/thing: if its note exists, READ it and append only the genuinely
   new facts — a bullet under `## About` and a dated line under `## Mentions`. NEVER
   rewrite, re-order, or wholesale replace an existing note, never paste template
   scaffold (`## About`/`## Conversations`/`## Mentions`) into a note that already has
   it, and never duplicate a fact — each `## Section` heading must appear exactly once
   per note. If the note does not exist, create it from the matching template.
3. If the conversation re-identifies a speaker (e.g. "Speaker 0" is actually Alice),
   rename `People/<old>.md` to `People/<new>.md` AND rewrite every `[[old]]` wikilink
   across the vault (`notesmd-cli move "People/<old>.md" "People/<new>.md"` does both in
   one shot if installed; otherwise grep for the links and edit each file). If the target
   note already exists, merge the old note's fact bullets into it instead, delete the old
   note, and rewrite the links.
4. HARD RULES: `Unknown Speaker N` is a diarization placeholder, not a person — never
   put it in `people:`, create a note for it, or wikilink it. Hermes is Chronicle's
   voice assistant, not a human — link it as the topic [[Hermes]]; never create or
   update `People/Hermes.md`. Never write captured content into Templates/ (category
   minting is the only Templates/ write). Never touch files outside this directory,
   never run git commands, never create scratch/plan files, and never delete a note
   except when merging a rename.
5. Work until everything is recorded. Your FINAL message must be only a 1-2 sentence
   summary of what you changed.

Be precise and conservative: capture what was actually said, link things, avoid invention.
{{vault_summary}}"""
)


def _codex_settings() -> dict:
    """The ``memory.codex`` mapping from config.yml (soft dependency — {} if absent)."""
    try:
        from advanced_omi_backend.model_registry import get_models_registry

        reg = get_models_registry()
        mem = (reg.memory if reg else None) or {}
        cfg = mem.get("codex") or {}
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception as e:  # noqa: BLE001 — registry optional (tests, host scripts)
        logger.debug("model registry unavailable for codex settings (%s)", e)
        return {}


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def codex_executor_available() -> tuple[bool, str]:
    """Whether the Codex CLI executor can run: binary on PATH + subscription auth."""
    binary = shutil.which(os.environ.get("CODEX_BINARY", "codex"))
    if not binary:
        return False, "codex binary not found on PATH"
    auth = _codex_home() / "auth.json"
    if not auth.is_file():
        return False, f"no Codex auth at {auth} (run `codex login` / mount CODEX_HOME)"
    return True, binary


class CodexMemoryAgent:
    """Runs one ``codex exec`` over the vault to turn a transcript into vault edits."""

    def __init__(
        self,
        vault_root: Path,
        operation: str = "memory_agent",
        *,
        force_fallback: bool = False,
    ):
        # `operation` is accepted for signature-compatibility with MemoryAgent; the
        # Codex executor doesn't use the model registry for its own calls.
        self.root = Path(vault_root)
        self.operation = operation
        self.force_fallback = force_fallback

    async def run(
        self,
        transcript: str,
        conversation_id: str,
        *,
        date: Optional[str] = None,
        duration_minutes: Optional[float] = None,
        title: Optional[str] = None,
        vault_summary: str = "",
        guidance: str = "",
    ) -> MemoryAgentResult:
        if self.force_fallback:
            # Note-guarantee recovery: the Codex run already failed to produce a valid
            # conversation note — retry through the direct agent on the fallback LLM
            # rather than re-running the same CLI.
            from .memory_agent import MemoryAgent

            logger.warning(
                "codex agent recovery for conv=%s: delegating to the direct memory "
                "agent (fallback LLM)",
                conversation_id,
            )
            return await MemoryAgent(self.root, force_fallback=True).run(
                transcript,
                conversation_id,
                date=date,
                duration_minutes=duration_minutes,
                title=title,
                vault_summary=vault_summary,
                guidance=guidance,
            )

        available, detail = codex_executor_available()
        if not available:
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=0,
                touched=[],
                summary="",
                errors=[f"codex executor unavailable: {detail}"],
                truncated=True,
            )
        binary = detail

        quota_payload, quota_block = await asyncio.to_thread(
            self._check_quota, conversation_id
        )
        if quota_block:
            from .memory_agent import MemoryAgent

            return await MemoryAgent(self.root).run(
                transcript,
                conversation_id,
                date=date,
                duration_minutes=duration_minutes,
                title=title,
                vault_summary=vault_summary,
                guidance=guidance,
            )

        date = date or datetime.now(timezone.utc).isoformat()
        system_prompt = await _get_prompt(
            CODEX_AGENT_SYSTEM_PROMPT_ID,
            DEFAULT_CODEX_AGENT_SYSTEM_PROMPT,
            vault_summary,
        )
        guidance_block = f"\n\n{guidance}" if guidance else ""
        prompt = (
            f"{system_prompt}\n\n"
            f"New conversation to record.\n"
            f"conversation_id: {conversation_id}\n"
            f"date: {date}\n"
            f"duration_minutes: {duration_minutes if duration_minutes is not None else 'unknown'}\n\n"
            f"source_title: {title or 'unknown'}\n\n"
            f"Transcript (speaker-labelled):\n{transcript}"
            f"{guidance_block}"
        )

        settings = _codex_settings()
        timeout = int(settings.get("timeout_seconds") or DEFAULT_RUN_TIMEOUT_SECONDS)
        sandbox_mode = str(settings.get("sandbox_mode") or "workspace-write")
        model = str(settings.get("model") or "")
        reasoning_effort = str(settings.get("reasoning_effort") or "")

        from ..vault_lock import VaultLockTimeout

        try:
            from advanced_omi_backend.observability.otel_setup import get_tracer

            tracer = get_tracer("chronicle.memory.codex")
            if tracer is None:
                return await asyncio.to_thread(
                    self._run_locked,
                    binary,
                    prompt,
                    conversation_id,
                    timeout,
                    sandbox_mode,
                    model,
                    reasoning_effort,
                )

            attributes = {
                "openinference.span.kind": "AGENT",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": "openai_codex_cli",
                "gen_ai.request.model": model or "codex-default",
                "gen_ai.conversation.id": conversation_id,
                "chronicle.memory.executor": "codex",
                "chronicle.memory.sandbox_mode": sandbox_mode,
                "chronicle.memory.transcript_chars": len(transcript),
                **codex_quota.quota_span_attributes(
                    quota_payload, str(settings.get("limit_id") or "")
                ),
                "langfuse.observation.input": json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "title": title,
                        "date": date,
                        "duration_minutes": duration_minutes,
                        "transcript_chars": len(transcript),
                    },
                    default=str,
                ),
            }
            with tracer.start_as_current_span(
                "codex_memory_agent", attributes=attributes
            ) as span:
                # asyncio.to_thread propagates the active context. The Redis run lock
                # and subprocess both live in that thread, while this child span remains
                # nested under the memory_extraction job in Langfuse.
                result = await asyncio.to_thread(
                    self._run_locked,
                    binary,
                    prompt,
                    conversation_id,
                    timeout,
                    sandbox_mode,
                    model,
                    reasoning_effort,
                )
                span.set_attribute("chronicle.memory.rounds", result.rounds)
                span.set_attribute("chronicle.memory.tool_calls", result.tool_calls)
                span.set_attribute(
                    "chronicle.memory.touched_count", len(result.touched)
                )
                span.set_attribute(
                    "chronicle.memory.removed_count", len(result.removed)
                )
                span.set_attribute("chronicle.memory.error_count", len(result.errors))
                span.set_attribute("chronicle.memory.truncated", result.truncated)
                # Mirrored onto the agent span for at-a-glance filtering; the
                # ingestable copy lives on the child codex_turn span (see
                # _record_usage_span for why it cannot live here).
                for key, value in result.usage.items():
                    span.set_attribute(f"chronicle.memory.usage.{key}", value)
                span.set_attribute(
                    "langfuse.observation.output",
                    json.dumps(
                        {
                            "summary": result.summary,
                            "touched": result.touched,
                            "removed_count": len(result.removed),
                            "rounds": result.rounds,
                            "tool_calls": result.tool_calls,
                            "errors": result.errors,
                            "truncated": result.truncated,
                        },
                        default=str,
                    ),
                )
                if result.errors:
                    span.set_attribute("error.type", "CodexMemoryAgentError")
                    span.set_attribute(
                        "error.message", "; ".join(result.errors)[-2000:]
                    )
                return result
        except VaultLockTimeout as e:
            return MemoryAgentResult(
                conversation_id=conversation_id,
                rounds=0,
                touched=[],
                summary="",
                errors=[str(e)],
                truncated=True,
            )

    # ------------------------------------------------------------------
    # subprocess + diff (sync; runs in a worker thread under the run lock)
    # ------------------------------------------------------------------

    def _run_locked(
        self,
        binary: str,
        prompt: str,
        conversation_id: str,
        timeout: int,
        sandbox_mode: str,
        model: str,
        reasoning_effort: str,
    ) -> MemoryAgentResult:
        import subprocess

        from ..vault_lock import vault_run_lock

        with vault_run_lock(self.root.name, ttl_seconds=timeout + 60):
            before = self._snapshot()
            with tempfile.NamedTemporaryFile(
                mode="r", suffix=".txt", prefix="codex-last-msg-", delete=False
            ) as last_msg_file:
                last_msg_path = Path(last_msg_file.name)
            cmd = [
                binary,
                "exec",
                "--json",
                "--skip-git-repo-check",  # the vault is not a git repository
                "--ephemeral",  # don't persist session files into CODEX_HOME
                "--cd",
                str(self.root),
                "--sandbox",
                sandbox_mode,
                "--output-last-message",
                str(last_msg_path),
            ]
            if model:
                cmd += ["-m", model]
            if reasoning_effort:
                cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
            cmd += ["-"]  # prompt on stdin (avoids ARG_MAX / quoting)

            env = {**os.environ, "RUST_LOG": os.environ.get("RUST_LOG", "error")}
            errors: List[str] = []
            stdout = ""
            timed_out = False
            logger.info(
                "codex agent starting for conv=%s (sandbox=%s model=%s timeout=%ds)",
                conversation_id,
                sandbox_mode,
                model or "default",
                timeout,
            )
            started_ns = time.time_ns()
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    cwd=str(self.root),
                )
                stdout = proc.stdout or ""
                if proc.returncode != 0:
                    errors.append(
                        f"codex exec exited {proc.returncode}: "
                        f"{(proc.stderr or '')[-_STDERR_TAIL_CHARS:].strip()}"
                    )
            except subprocess.TimeoutExpired as e:
                timed_out = True
                stdout = (
                    e.stdout.decode()
                    if isinstance(e.stdout, bytes)
                    else (e.stdout or "")
                )
                errors.append(f"codex exec timed out after {timeout}s")
            except OSError as e:
                errors.append(f"codex exec failed to start: {e}")

            ended_ns = time.time_ns()

            command_count, turn_count, event_errors, usage = self._parse_events(stdout)
            errors.extend(event_errors)
            self._record_usage_span(usage, model, started_ns, ended_ns)

            summary = ""
            try:
                summary = last_msg_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            finally:
                last_msg_path.unlink(missing_ok=True)

            after = self._snapshot()

        touched = sorted(
            rel for rel, content in after.items() if before.get(rel) != content
        )
        removed = [
            {"old_path": rel, "new_path": "", "before": before[rel]}
            for rel in sorted(before)
            if rel not in after
        ]
        failed = timed_out or (not summary and bool(errors))
        result = MemoryAgentResult(
            conversation_id=conversation_id,
            rounds=max(turn_count, 1),
            touched=touched,
            summary=summary,
            tool_calls=command_count,
            removed=removed,
            errors=errors,
            usage=usage,
            truncated=failed,
        )
        logger.info(
            "codex agent done: conv=%s turns=%d commands=%d touched=%d removed=%d "
            "errors=%d tokens=in:%d/cached:%d/out:%d%s — %s",
            conversation_id,
            result.rounds,
            command_count,
            len(touched),
            len(removed),
            len(errors),
            usage.get("input_tokens", 0),
            usage.get("input_cached_tokens", 0),
            usage.get("output_tokens", 0),
            " (FAILED)" if failed else "",
            summary[:160],
        )
        return result

    @staticmethod
    def _check_quota(conversation_id: str) -> tuple[Optional[dict], bool]:
        """Return the quota snapshot and whether this run should yield the budget.

        Chronicle's background recording shares one account-wide weekly budget with
        the user's interactive Codex sessions, and is the cheaper consumer to give
        up: a yielded run still records the conversation via the direct (metered
        API) executor, while a blocked interactive session is stuck for days.

        Fails OPEN — an unreadable quota yields ``False`` and the run proceeds. The
        probe is an optimisation over Codex's own limit error, not a correctness
        gate, so a broken probe must not stop memory extraction entirely.
        """
        settings = _codex_settings()
        threshold = settings.get("max_used_percent")
        if threshold is None:
            return None, False
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            logger.warning("ignoring non-numeric memory.codex.max_used_percent")
            return None, False

        limit_id = str(settings.get("limit_id") or "")
        payload = codex_quota.read_rate_limits()
        used = codex_quota.bucket_used_percent(payload, limit_id)
        if used is None:
            logger.debug("codex quota unknown for conv=%s; proceeding", conversation_id)
            return payload, False
        if used < threshold:
            return payload, False

        logger.warning(
            "codex quota %d%% used (>= %d%% budget for Chronicle) — recording conv=%s "
            "via the direct memory agent instead, leaving the remainder for "
            "interactive use",
            used,
            threshold,
            conversation_id,
        )
        return payload, True

    @staticmethod
    def _record_usage_span(
        usage: Dict[str, int], model: str, started_ns: int, ended_ns: int
    ) -> None:
        """Emit the model call as a child LLM span carrying the run's token usage.

        Deliberately NOT on the parent ``codex_memory_agent`` span: current Langfuse
        drops usage from spans whose ``gen_ai.operation.name`` is ``invoke_agent`` or
        ``agent_step`` (it assumes the agent span duplicates usage from child
        model-call spans) and would ingest the tokens as zero without erroring. Older
        Langfuse — including 3.x — has no such guard, so putting usage on the agent
        span works today and silently breaks on upgrade. A child model-call span is
        correct under both.

        Created after the subprocess returns, since usage is only known then, with
        explicit timestamps so it still spans the real call window.
        """
        if not usage:
            return
        try:
            from advanced_omi_backend.observability.otel_setup import get_tracer

            tracer = get_tracer("chronicle.memory.codex")
            if tracer is None:
                return
            attributes = {
                "openinference.span.kind": "LLM",
                "gen_ai.operation.name": "chat",
                "gen_ai.system": "openai",
                "gen_ai.provider.name": "openai_codex_cli",
                "gen_ai.request.model": model or "codex-default",
                "gen_ai.response.model": model or "codex-default",
                **{f"gen_ai.usage.{k}": v for k, v in usage.items()},
            }
            span = tracer.start_span(
                "codex_turn", attributes=attributes, start_time=started_ns
            )
            span.end(end_time=ended_ns)
        except Exception:  # noqa: BLE001 - telemetry must never fail the run
            logger.debug("failed to record codex usage span", exc_info=True)

    def _snapshot(self) -> Dict[str, str]:
        """Vault-relative ``*.md`` contents (same shape the provider's audit diff uses)."""
        snapshot: Dict[str, str] = {}
        if not self.root.exists():
            return snapshot
        for path in self.root.rglob("*.md"):
            try:
                snapshot[path.relative_to(self.root).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
        return snapshot

    @staticmethod
    def _parse_events(stdout: str) -> tuple[int, int, List[str], Dict[str, int]]:
        """Tolerantly scan the ``--json`` JSONL stream for counts, errors, and usage.

        ``turn.completed`` carries the turn's token ``usage``; it is the only place
        the CLI reports what a run actually cost, so it is summed across turns and
        translated into Langfuse's usage-detail key names.
        """
        commands = 0
        turns = 0
        errors: List[str] = []
        usage: Dict[str, int] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = str(event.get("type", ""))
            item = event.get("item") or {}
            item_type = str(item.get("item_type") or item.get("type") or "")
            if etype == "item.completed" and "command" in item_type:
                commands += 1
            elif etype == "turn.completed":
                turns += 1
                for key, value in CodexMemoryAgent._turn_usage(event).items():
                    usage[key] = usage.get(key, 0) + value
            elif etype == "turn.failed":
                turns += 1
                failure = event.get("error") or {}
                errors.append(f"codex turn failed: {failure.get('message', failure)}")
            elif etype == "error":
                errors.append(f"codex error: {event.get('message', event)}")
        return commands, turns, errors, usage

    @staticmethod
    def _turn_usage(event: dict) -> Dict[str, int]:
        """Map one ``turn.completed`` event's ``usage`` to Langfuse usage details.

        Tolerant by design: the CLI's field names are not a stable contract, so an
        absent or oddly-shaped block yields ``{}`` rather than failing the run.
        """
        raw = event.get("usage")
        if not isinstance(raw, dict):
            return {}
        # Codex reports cached input tokens *inside* input_tokens, which is what
        # Langfuse's normaliser assumes (it derives uncached input as
        # input_tokens - input_cached_tokens), so both pass through unchanged.
        # Caveat on Langfuse 3.x: it instead adds the two, so the rollup `usage.input`
        # and `total` over-count cached tokens there. `usageDetails.input` is right
        # on both.
        field_map = {
            "input_tokens": "input_tokens",
            "cached_input_tokens": "input_cached_tokens",
            "output_tokens": "output_tokens",
            "reasoning_output_tokens": "output_reasoning_tokens",
        }
        usage: Dict[str, int] = {}
        for source, target in field_map.items():
            value = raw.get(source)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            usage[target] = int(value)
        return usage
