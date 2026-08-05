# Memory Service: The Agentic Markdown Vault

> 📖 **Prerequisite**: Read the [Quick Start Guide](../../quickstart.md) first for system overview.

This document explains how Chronicle stores and retrieves memories.

Chronicle has **one** memory provider: `chronicle`. It is an **agentic Markdown vault** — a directory of Obsidian-style notes that is the single source of truth for memories. There is no separate vector database, no embeddings, and no hybrid search index. Memories are plain Markdown files; both writing and reading are driven by tool-calling LLM agents.

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
│  Write agent (_add_memory_  │   tool-calling LLM
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
│  Read agent (_search_vault_ │   tool-calling LLM
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

## Write path: the memory agent

Memory extraction runs as part of the post-conversation RQ pipeline. After a conversation closes, `memory_extraction_job` calls `memory_service.add_memory()`, which invokes the **write agent** (`_add_memory_agent` in `providers/chronicle.py`).

The write agent is a **tool-calling LLM**. Given the conversation transcript and metadata, it:

1. Records the conversation as a new `Conversations/<conversation_id>.md` note.
2. **Surgically edits** existing People / Topics / Category notes — adding or updating facts in place rather than blindly appending — and creates new notes when a person/topic/category is seen for the first time.

This is LLM-driven extraction: the agent decides what is worth remembering and where it belongs in the vault.

## Read path: the retrieval agent

Search is served by the **read agent** (`_search_vault_grep`), a read-only tool-calling LLM that operates over the vault with three tools:

- `grep` — full-text ripgrep across the notes
- `glob` — find notes by path/name pattern
- `read_note` — read a specific note's contents

Given a query, the agent greps the vault, reads the relevant notes, and **synthesizes an answer**. The result returned to the caller is:

- the **synthesized answer** as the top result, plus
- the **notes it read** (cited note paths) as supporting context.

There is no vector similarity score — relevance comes from the agent's reasoning over the text it retrieves.

## Chat integration

Chat is always **agentic / tool-calling**. The chat LLM is given a `search_memories` tool; when it needs context about the user it calls that tool, which runs the same agentic vault search and returns the synthesized answer plus the cited note paths. The chat model then incorporates that into its reply.

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

The memory provider is `chronicle` and requires only an LLM (for the write and read agents) and the vault directory. LLM selection follows the standard backend LLM configuration (`LLM_PROVIDER`, `OPENAI_API_KEY`/`OPENAI_MODEL` or Ollama settings) in `config/config.yml` and `.env`. No vector store, embedding model, or graph database needs to be configured.
