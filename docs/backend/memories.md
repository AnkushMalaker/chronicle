# Memory Service: The Agentic Markdown Vault

> 📖 **Prerequisite**: Read the [Quick Start Guide](../../quickstart.md) first for system overview.

This document explains how Chronicle stores and retrieves memories.

Chronicle has **one** memory provider: `chronicle`. It is an **agentic Markdown vault** — a directory of Obsidian-style notes that is the single source of truth for memories. There is no separate vector database, no embeddings, and no hybrid search index. Memories are plain Markdown files; writing and reading each have an independently selectable agent backend.

**Code References**:
- **Provider**: `src/advanced_omi_backend/services/memory/providers/chronicle.py`
- **Memory agents**: `src/advanced_omi_backend/services/memory/agent/` (write agent + read/retrieval agent)
- **Memory extraction job**: runs in the post-conversation RQ chain (`memory_extraction_job`), calls `memory_service.add_memory()`
- **Configuration**: `config/config.yml` (memory + LLM sections) + `src/model_registry.py`

## Overview

```
Conversation transcript
        │
        ▼ memory_extraction_job → memory_service.add_memory()
┌─────────────────────────────┐
│  Write agent (_add_memory_  │   direct / Codex / Pi
│  agent)                     │   • record conversation note
│                             │   • surgically edit People/
│                             │     Topics/Category notes
└──────────────┬──────────────┘
               ▼
   data/conversation_docs/<user_id>/   ← the vault (source of truth)
        Conversations/<id>.md
        People/<name>.md
        Topics/<topic>.md
        <Category>/<name>.md
               ▲
               │ ripgrep (grep / glob / read_note tools)
┌──────────────┴──────────────┐
│  Read agent (_search_vault_ │   direct / Pi
│  grep)                      │   • greps the vault
│                             │   • reads relevant notes
│                             │   • synthesizes an answer
└─────────────────────────────┘
               ▲
   /api/memories/search   and   chat `search_memories` tool
```

## Storage: the vault layout

The vault lives on disk at:

```
data/conversation_docs/<user_id>/
```

It is per-user (keyed by the MongoDB ObjectId `user_id`) and organized into note types:

| Note type | Path | Contents |
|-----------|------|----------|
| **Conversations** | `Conversations/<conversation_id>.md` | One note per conversation — the record of what was discussed. |
| **People** | `People/<name>.md` | A note per person mentioned, accumulating facts about them over time. |
| **Topics** | `Topics/<topic>.md` | A note per recurring topic. |
| **Categories** | `<Category>/<name>.md` | Other category notes (e.g. places, projects, preferences). |

These are ordinary Markdown files — readable, editable, and grep-able. Because the vault is the system of record, memories survive as durable text rather than as opaque vector rows.

If an Immich photo library is configured (`IMMICH_URL`/`IMMICH_API_KEY`, offered by the setup wizard), the `person_photos` cron job (`services/person_photos.py`) matches each `People/<name>.md` note against Immich's people API, stores the person's face-crop thumbnail content-addressed under the vault's `_media/` directory, and embeds a small photo at the top of the note.

## Agent backends

The write and search paths are selected independently under `memory.agents`:

| Backend | Write | Search | Model/auth source |
|---|---:|---:|---|
| `direct` | Yes | Yes | Built-in tool-calling loop using the model resolved by `llm_operations.memory_write` or `memory_search`. |
| `codex` | Yes | No | Codex CLI and ChatGPT subscription auth from the `CODEX_HOME` mount. |
| `pi` | Yes | Yes | Pi CLI using a Chronicle model-registry entry; local llama.cpp, Ollama, and remote OpenAI-compatible models all use the same path. |

Writes also declare `recovery_backend`. It defaults to `direct`, so a failed Codex or
Pi run gets one direct-agent recovery attempt. When direct is already the primary,
the recovery attempt uses `defaults.fallback_llm`. Set it to `null` to disable agent
recovery. The setup wizard preserves an explicitly configured value on reruns rather
than silently resetting it to `direct`.

Codex subscription authentication is also a readiness requirement when Codex is the
configured primary writer. A recovery backend handles a write attempt that fails after
the service is ready; it does not make an unauthenticated Codex primary ready. Run
`codex login` in the host `CODEX_HOME` before starting that configuration.

Pi is installed in the backend image and runs non-interactively with isolated runtime
configuration. Chronicle resolves `memory.backends.pi.model` through its model
registry, including the upstream model ID, URL, and API key. No host-side Pi login,
`~/.pi` directory, auth volume, or hand-written `models.json` is required. The shipped
default is Pi 0.83.0 on Node 22.19.0.

The Pi process is not given Pi's built-in shell or filesystem tools. Chronicle disables
them and loads a generated extension containing only the canonical vault tool schemas;
calls cross a short-lived, bearer-authenticated loopback gateway into `VaultTools`.
The search extension receives only the read-only search schemas.

Write loops are bounded at 32 model/tool rounds. Pi additionally enforces an atomic
128-call write cap at the gateway. Search is bounded at 6 tool rounds and 24 calls.
When that tool budget is exhausted, the direct backend gets exactly one completion with
no tool schemas so it can synthesize from evidence already in its conversation. Pi gets
one fresh, isolated no-tool process only when it has already read note evidence; that
process receives the selected evidence but no Chronicle extension or Pi built-in tools.
Neither final-synthesis path can perform another vault operation. A
truncated, stalled, timed-out, or process-failed write retains every audited partial
mutation but is not reported as complete: Chronicle invokes the configured recovery
backend even when the partial run already produced a valid conversation note. New
People and Topic notes are also checked at the tool boundary for every canonical
section and aggregation embed, so a smaller local model gets a recoverable tool error
instead of silently leaving a malformed long-lived note.

## Write path: the memory agent

Memory extraction runs as part of the post-conversation RQ pipeline. After a conversation closes, `memory_extraction_job` calls `memory_service.add_memory()`, which invokes the **write agent** (`_add_memory_agent` in `providers/chronicle.py`).

Given the conversation transcript and metadata, the selected write backend:

1. Records the conversation as a new `Conversations/<conversation_id>.md` note.
2. **Surgically edits** existing People / Topics / Category notes — adding or updating facts in place rather than blindly appending — and creates new notes when a person/topic/category is seen for the first time.

This is LLM-driven extraction: the agent decides what is worth remembering and where it belongs in the vault.

### Capture evidence is remembered by the day, not the conversation

Continuous ScreenPipe audio does not take this path. It is assembled into bounded compute spans — 30 minutes, or two hours given a collector meeting interval — so one meeting can span several recordings, and remembering per recording would inherit those arbitrary cuts. A timeline episode already carries the semantic bounds.

`add_day_memory` therefore records one **settled local day** of episodes in a single write, anchored on `Daily/<local_date>.md` rather than under `Conversations/`, which stays one note per conversation. Durable People/Topic/Category edits are unchanged, and the write shares the conversation path's executor selection, recovery backend, bounded rounds, Langfuse spans, and audit ledger (`MemoryCause.DAY_EPISODES`).

Unlike the conversation path there is no deterministic source-preserving fallback: a conversation note can always be written from its transcript, but a day has no such artifact. A day that cannot be written stays unwritten and is retried, then settles into `skipped` with its diagnostic.

A ScreenPipe recording that the timeline agent judged **conversational** — a standup, a 1:1 — is separately promoted back into the Recordings list and search. See [Semantic timeline episodes](timeline-episodes.md#a-conversational-episode-promotes-the-recordings-it-cites).

### Checking the write: structure, then a reviewer

A completed write is checked twice before the run is accepted, because the two things
that go wrong are not the same kind of thing.

**Structure** is decided by a function. `vault_verify.verify_vault_changes` diffs the
vault against a pre-run snapshot and reports illegal paths, a note missing its canonical
sections or aggregation embed, a newly duplicated `## Section`, a case-only collision, a
day write that minted a `Conversations/` note, and a run that never wrote the record note
it was asked for. Each `Finding` carries a fix instruction addressed to a model. The same
function is offered to the agent as the `verify_vault` tool so it can self-correct
in-run, and re-run server-side so correctness does not depend on it choosing to.

**Redundancy cannot be.** Structural verification passes on a perfectly well-formed
bullet that re-records something the vault already holds — which is exactly how a
DeepSeek V4 Pro day write finished with *Vault verification passed* after restating the
phone stand, the chai, and the air-fryer fries that `People/ankush.md` and
`People/anushpa.md` already carried. Deciding that means reading the surrounding notes
and judging whether two differently worded sentences carry the same fact, so a second
agent does it (`agent/review_agent.py`):

- **read-only** — `grep`/`glob`/`read_note` and a `report_findings` tool, so a review
  cannot mutate the vault it judges;
- **fresh context** — it sees the source and the lines actually added, never the
  writer's reasoning, so it cannot inherit the writer's conviction that the work was
  done;
- **narrow** — only `redundant` (a note of the same kind already records this) and
  `unsupported` (the source does not say this). Off-vocabulary verdicts are dropped;
  `Daily`, `People`, and `Topics` overlap on purpose and that overlap is not redundancy;
- **never judging what it cannot see** — the source is bounded at the day digest's own
  budget, and if it still had to be cut, `unsupported` is withdrawn for that run. A
  reviewer shown part of a source cannot tell "the source never said this" from "the
  source said it in the part you were not given", and left to judge anyway it picks the
  former in confident detail: cutting a 39,563-char digest at 24,000 hid a gaming
  session, and a true bullet about it was flagged as invented;
- **advisory** — its findings are the same `Finding` type and flow into the same bounded
  repair pass. A reviewer that fails, stalls, or returns nothing parseable yields no
  findings, because a broken reviewer must never block a good write.

It ends with a **forced verdict**: when the round or tool-call budget runs out, the
search tools are withdrawn and the model is asked to report from what it has already
read. Measured on the live vault, that step is what makes the reviewer usable at all —
the first runs exhausted six rounds with the right answer already written in their own
prose ("no mention of Tokyo … this is unsupported") and returned nothing, because they
never got a round in which to report. With nothing left to call but the verdict, there
is no next search to narrate.

Measured on the live vault with `scripts/probe_write_review.py`, which injects bullets
whose verdict is known into a copy of the real notes: **28/32** over two days and eight
trials on Qwen 3.6 27B. Genuinely-new bullets were left alone 8/8 and invented ones
caught 8/8. All four misses are the same borderline bullet, where the reviewer finds the
overlap and then rules it a *new detail about an already-recorded event* — the exception
its own instructions grant — rather than a duplicate.

Disable per deployment with `memory.agents.write.review: false`; it costs one extra
agent run per write that changed anything (~38s against a ~190s day write here).

## Read path: the retrieval agent

Search is served by the **read agent** (`_search_vault_grep`). Both the direct and Pi
search backends are read-only and operate over the vault with four tools:

- `grep` — full-text ripgrep across the notes
- `glob` — find notes by path/name pattern
- `read_note` — read a specific note's contents
- `search_images` — rank saved images by what they *look* like, returning `Manual Memories/`
  note paths for `read_note`. Backed by the optional
  [ColPali service](../manual-memories.md#enrichment-and-recovery); when that is
  unreachable it returns a plain sentence saying so rather than raising, and the
  images remain findable by grep over their descriptions.

Given a query, the agent greps the vault, reads the relevant notes, and **synthesizes an answer**. The result returned to the caller is:

- the **synthesized answer** as the top result, plus
- the **notes it read** (cited note paths) as supporting context.

There is no vector similarity score — relevance comes from the agent's reasoning over the text it retrieves.

## Chat integration

Chat is always **agentic / tool-calling**. The chat LLM is given a `search_memories` tool; when it needs context about the user it calls that tool, which runs the same agentic vault search and returns the synthesized answer plus the cited note paths. The chat model then incorporates that into its reply.

## Langfuse tracing

When Chronicle's existing `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, and
`LANGFUSE_SECRET_KEY` variables are configured, memory work is exported to the same
local Langfuse OTLP endpoint as the rest of the pipeline. A write trace shows primary
and recovery attempts, the selected executor, model calls, canonical vault-tool calls,
deterministic fallback, latency, token usage, and completion state. A search trace shows
the executor, rounds, tool calls, notes-read count, cap recovery, warnings, usage, and
whether the final answer was usable. Pi's Node subprocess emits equivalent manual model
usage spans, so it is visible beside Direct and Codex rather than becoming a telemetry
blind spot. Langfuse OTLP export is batched, keeping network export off the vault-tool
mutation path even when an agent emits many tool observations. Chronicle explicitly
flushes the completed trace tree before a forked RQ work-horse exits.

Chronicle's manual memory spans are metadata-only by default: they retain lengths and
SHA-256 fingerprints but omit transcripts, queries, answers, note paths/bodies, tool
arguments, and raw provider errors. Set `LANGFUSE_MEMORY_CAPTURE_CONTENT=true` only
when Langfuse is trusted and local and that personal content is useful for a bounded
debugging session. Content-bearing fields are length-limited; API keys, model endpoints,
and Pi gateway tokens are never emitted by the memory tracer.

The Direct executor also has native child spans from Chronicle's global OpenInference
OpenAI instrumentation. Those spans have independent privacy controls and include model
inputs/outputs by default. Set both `OPENINFERENCE_HIDE_INPUTS=true` and
`OPENINFERENCE_HIDE_OUTPUTS=true` to redact them; because instrumentation is global,
those settings apply to every OpenAI-client call in Chronicle, not only memory. Pi and
Codex subprocess spans use the memory-specific content toggle above.

## API Endpoints

- `GET /api/memories/search?query={query}&limit={limit}` — runs the agentic vault search and returns the synthesized answer plus the notes the read agent consulted.
- `GET /api/memories/people/suggestions` — ranks conservative deterministic duplicate-person candidates for review; it never merges automatically.
- `POST /api/memories/people/identity` — records or clears a symmetric `distinct_from` decision in two People notes, with optional stale-revision protection.
- `POST /api/memories/people/merge/preview` and `POST /api/memories/people/merge` — preview and apply a locked deterministic merge. A `distinct_from` decision blocks preview.
- Other `/api/memories/*` management endpoints operate over the vault notes.

## Vault sync to Obsidian (separate feature)

The vault is designed to be edited and viewed directly. The optional **vault sync** feature (in the cross-platform desktop tray, `extras/chronicle-tray/`) syncs `data/conversation_docs/` to an Obsidian vault via Syncthing, so you can browse and hand-edit your memory notes in Obsidian. Human edits made in Obsidian sync back into the vault. This sync is independent of the memory provider itself — the vault on the backend remains the source of truth.

The optional [Chronicle Companion](../obsidian-companion.md) plugin adds explicit,
deterministic maintenance actions such as merging duplicate people. The UI previews and
confirms the action, while the backend performs the locked mutation; no LLM participates
in execution.

## What was removed

For historical context, the previous architecture used **FalkorDB** hybrid search (vector + BM25 + entity-graph BFS over ConvDoc/ConvChunk/ConvEntity nodes and a knowledge graph), plus alternative providers (OpenMemory MCP, Graphiti) and Qdrant/Mem0 vector storage. **All of these have been removed.** There is now a single `chronicle` provider backed entirely by the Markdown vault; the `falkordb` container and `FALKORDB_*` environment variables no longer exist.

## Configuration

The setup wizard asks for write and search backends separately. This nested structure
is the only supported configuration shape:

```yaml
memory:
  provider: chronicle
  timeout_seconds: 1200
  agents:
    write:
      backend: pi
      recovery_backend: direct
      review: true            # read-only review agent over what the write added
    search:
      backend: pi
  backends:
    direct: {}
    codex:
      model: gpt-5.6-terra
      reasoning_effort: low
      sandbox_mode: workspace-write
      timeout_seconds: 900
      max_used_percent: 80
      limit_id: ""
    pi:
      model: muse-glimmer-llm  # Chronicle model-registry entry, not upstream model ID
      timeout_seconds: 900
      context_window: 131072
      max_tokens: 4096        # capped generally; leaves most context for prompts/tools
      thinking: high

llm_operations:
  memory_write:
    reasoning_effort: high
    max_tokens: 8000
  memory_search:
    reasoning_effort: high
    max_tokens: 8000
```

The Pi model may be any OpenAI-compatible LLM entry in the effective registry formed by
`config/defaults.yml` plus name-based overrides from `config/config.yml`. The wizard
rejects missing entries, embeddings, and non-OpenAI API families. For the local Muse
Glimmer service, setup selects `muse-glimmer-llm`, records llama.cpp's exact upstream
Hugging Face identity, and records the context actually selected for that service. API credentials,
when a selected registry model needs them, continue to come from the model definition's
environment-variable reference.
No vector store, embedding model, or graph database is part of memory storage or
retrieval.

Pi limits are derived per selected model rather than pinning every backend to one
machine's context profile. The wizard uses a declared `context_window` (including the
context written by local llama.cpp setup), otherwise a conservative 32K fallback. New
output limits are one quarter of the context up to 4096 tokens, leaving most of the
window for Pi's system prompt, tool schemas, transcript, and multi-round results.
Existing explicit Pi limits are preserved on rerun.

The built-in Qwen 3.6 27B profile serves a 64K context with one parallel slot, flash
attention, Q8_0 K/V cache, and Jinja chat templates. It also disables llama.cpp's
automatic multimodal-projector download: Chronicle's memory workload is text-only, so
loading its projector wastes memory. On the A30, 64K Q8 KV used 19,482 MiB with the
auto-loaded projector and 18,344 MiB with the text-only profile, freeing about 1.1 GiB;
32K Q8 used 18,234 MiB with the projector. Prompt processing stayed near 667
tokens/second. Audited memory prompts reached roughly 15K tokens, making the old 8K
profile invalid for real conversations. Other local models retain llama.cpp's safer
automatic flash-attention, F16 KV-cache, and projector defaults. Increase the served
context before raising output limits or enabling thinking, and validate changes with
the benchmark harness.
