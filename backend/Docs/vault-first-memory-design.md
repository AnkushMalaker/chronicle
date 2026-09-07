# Vault-First Memory — Design & Plan

**Status: SHIPPED** — this migration is complete and live. The Markdown vault is now the
**single** memory store. FalkorDB (hybrid vector + BM25 + entity graph), its per-user
graph, ConvDoc/ConvChunk/ConvEntity nodes, memory embeddings, the Knowledge Graph
entity/relationship service, the Obsidian-zip-ingest service, OpenMemory MCP, Graphiti,
and Qdrant have all been **removed entirely**. Write is a tool-calling memory agent;
retrieval (and chat) is an agentic, read-only ripgrep search over the vault. The sections
below preserve the original design rationale and the (now-completed) rollout plan; treat
any present-tense "we want to" / "behind a toggle" language as historical.

**Branch context:** `fix/optimizations-2` · **Author:** design session, 2026-06-03

## 1. The idea in one paragraph

Chronicle already writes every conversation as a markdown file (an Obsidian-style
"vault"). Originally that file was a *byproduct* — the real index was FalkorDB (vector +
BM25 + entity graph). This design flipped it, and that flip has now shipped: **the vault
is the system of record and the only retrieval surface**, FalkorDB has been removed
entirely, and retrieval happens by an agentic ripgrep search over the vault (the LLM
formulates the regex) plus a *vault-aware system prompt* and vault-navigation tools so it
finds things the way a human power-user would — open the person note, read their
conversations, jump to a section. The vault gets the Kepano treatment: people become their
own notes, links are expressed as frontmatter properties, and `.base` files aggregate
everything.

The bet (now validated): a well-structured vault + a good "here's how the vault is laid
out" prompt + fast grep retrieves more transparently than re-embedding everything, and
it's debuggable (you can open it in Obsidian) and portable.

## 2. State at design time (historical — grounding for the plan below)

> The flow below is the **pre-migration** pipeline that motivated this design. Steps 4–5
> (embeddings + FalkorDB graph) have since been **removed**; the write path is now a
> tool-calling memory agent (`services/memory/agent/`). See §7 for what actually shipped.

Post-transcription flow (`controllers/queue_controller.py:489` → RQ chain):

```
recognise_speakers_job → process_memory_job → generate_title_job
                                              → generate_short_summary_job
                                              → generate_detailed_summary_job → dispatch_complete
                              │
                              ▼  memory_jobs.py:135 → memory_service.add_memory()
                         providers/chronicle.py:325
   1. LLM → structured markdown   (_generate_conversation_doc, prompt "memory.generate_conversation_doc")
   2. parse_conversation_doc()
   3. vault.write_doc()           → {DATA_DIR}/conversation_docs/{user_id}/{conv_id}.md   ← ground truth
   4. generate_embeddings()       ← REMOVED
   5. _store_in_graph()           → REMOVED (was ConvDoc ─HAS_CHUNK→ ConvChunk ; ConvDoc ─MENTIONS→ ConvEntity)
```

Current vault file (`vault_manager.py`, prompt at `prompt_defaults.py:647`):

```yaml
---
conversation_id: <uuid>
date: <iso>
speakers: [Alice, Bob]      # plain strings — NOT links
duration_minutes: <n>
---
## Title
### Summary
### Key Facts   - …verbatim WH-details…
### People      - Alice (colleague, ML eng)     # plain text, not a note
### Action Items - [ ] …
```

Retrieval consumers of `search_memories()` (all abstract through `MemoryServiceBase`, so
they keep working if we change the provider internals):

- `chat_service.py` chat is now **always** agentic tool-calling: the chat LLM calls a
  `search_memories` tool that runs the agentic vault search and returns a synthesized
  answer + cited note paths (no RAG pre-injection, no `memory_mode` toggle)
- `controllers/memory_controller.py:101` ← `/api/memories/search` (`memory_routes.py:55`)
- `workers/conversation_jobs.py` summary enrichment (`search_memories(transcript, …, limit=10)`)

Provider selection: `services/memory/config.py` (`MemoryConfig`, `MemoryProvider` enum,
`build_memory_config_from_env()` reading `get_models_registry().memory`) →
`service_factory.py:create_memory_service()`.

Prompts: `prompt_registry.py` (`get_prompt()` tries Langfuse override, falls back to
`prompt_defaults.py`). Conversation-doc prompt is `memory.generate_conversation_doc`.

> Note (historical): at design time there was a second FalkorDB-backed path —
> `services/obsidian_service.py` + `routers/modules/obsidian_routes.py` (the Obsidian-zip
> ingest) — that indexed vault notes into a per-user vector index. This plan superseded its
> retrieval role with grep-based search, and that service has since been **removed entirely**.

## 3. Target architecture

```
                       ┌──────────────────────────────────────┐
   transcription  ───► │ process_memory_job → add_memory()    │
                       │   1. LLM → markdown (person-aware)   │
                       │   2. write conversation note         │  notesmd-cli create / vault_manager
                       │   3. upsert People/*.md notes        │  notesmd-cli create (idempotent)
                       │   4. set people:/topics: properties  │  notesmd-cli frontmatter --edit
                       │   5. [graph storage]  ← TOGGLE OFF   │  guarded by graph_storage_enabled
                       └──────────────────────────────────────┘
                                        │
   ┌───────────────────── THE VAULT (ground truth) ─────────────────────┐
   │  Conversations/<conv>.md   People/<name>.md   Topics/<t>.md        │
   │  *.base (People, Conversations#Person, Topics)   hubs (People.md)  │
   └────────────────────────────────────────────────────────────────────┘
                                        │
                       ┌──────────────────────────────────────┐
   chat / search  ◄─── │ search_memories() = notesmd-cli grep │  search-content --format json
                       │ + agentic vault tools for chat LLM   │  print / list / search-content / frontmatter
                       │ + vault-map system prompt (editable) │  prompt_registry, user-updatable
                       └──────────────────────────────────────┘
```

Three independent workstreams, shippable in order:

1. **Toggle FalkorDB off** (smallest, unblocks "a few runs without graph").
2. **notesmd-cli integration layer** + grep-backed `search_memories()`.
3. **Kepano vault refinement** (person notes, property links, bases) + agentic retrieval + editable vault-map prompt.

---

## 4. Workstream 1 — Bench FalkorDB behind a toggle

Goal: run the pipeline with the vault written but **no embeddings, no graph writes, no
FalkorDB dependency at startup**.

### 4.1 Config flag

`services/memory/config.py` — extend `MemoryConfig`:

```python
@dataclass
class MemoryConfig:
    memory_provider: MemoryProvider = MemoryProvider.CHRONICLE
    graph_storage_enabled: bool = True   # NEW — when False: skip embeddings + FalkorDB
    ...
```

`build_memory_config_from_env()` reads it from the models registry `memory` block (same
place `provider` comes from) and/or env `MEMORY_GRAPH_STORAGE=false`. Default stays
`True` so existing deployments are unchanged.

### 4.2 Guard points in `providers/chronicle.py`

| Location | Behavior when `graph_storage_enabled=False` |
|---|---|
| `_ensure_initialized()` / init probe (`~198–214`) | Skip FalkorDB ping + `_get_io()` + `_create_schema()` |
| `add_memory()` embeddings (`376–380`) | Skip — no embeddings generated |
| `add_memory()` delete+store (`388–410`) | Skip `DETACH DELETE` and `_store_in_graph`; return synthetic chunk ids `f"{source_id}_{i:03d}"` |
| `search_memories()` (`586–661`) | Route to vault grep (Workstream 2) instead of vector/BM25/BFS |
| `delete_all_user_memories()` (`~1038`) | Keep `vault.delete_all_docs()`, skip graph drop |
| `test_connection()` (`~1126`) | Return healthy (vault-only) without pinging FalkorDB |

Pattern:

```python
if self.config.graph_storage_enabled:
    chunk_ids = await asyncio.to_thread(self._store_in_graph, ...)
else:
    chunk_ids = [f"{source_id}_{i:03d}" for i in range(len(doc.sections))]
```

### 4.3 Import / infra de-coupling

- `services/graph_client.py:7` `from falkordb import ...` — make lazy (import inside the
  functions that use it) so a vault-only run doesn't require the package/service.
- `health_routes.py:270` — skip the FalkorDB memory health check when graph storage is off.
- `docker-compose.yml` `depends_on: falkordb` — leave as-is for now (running the container
  is cheap); the point is the *backend* no longer fails if FalkorDB is slow/absent. Revisit
  making the service itself optional later.

**Acceptance:** with the flag off, a conversation produces a vault file, no embedding API
calls, no FalkorDB writes; `/api/memories/search` and chat still return results (via grep).

---

## 5. Workstream 2 — notesmd-cli integration

`notesmd-cli` is a pure-filesystem Go binary (no Obsidian app needed), fully scriptable
(`--format json`, `--no-interactive`, exit codes). We use it as much as practical;
per-call subprocess cost is acceptable.

### 5.1 What we use it for — verified by spike 2026-06-03

Spike built the binary (`go build`) and tested every op against a realistic vault
(conversation note with `people:`/`topics:` wikilink lists + person notes). Verdict:

| Operation | Command | Verdict |
|---|---|---|
| Search vault | `search-content "<q>" --no-interactive --format json` | ✅ **Use.** Clean JSON; finds frontmatter links, inline mentions, and filename matches with line numbers + snippets |
| Read a note | `print "<note>" [--mentions]` | ✅ Use |
| List notes/folders | `list [path]` | ✅ Use |
| Create note (idempotent) | `create "<note>" --content "…"` (no flags = no-op if exists) | ✅ **Use.** Confirmed: no-flag create does NOT clobber an existing note — safe person-note upsert |
| **Rename person** | `move "<old>" "<new>"` (target absent) | ✅ **Use — headline win.** Renames file + rewrites `[[old]]`→`[[new]]` in every note's frontmatter AND body, leaving all other formatting byte-for-byte intact (pure textual link-replace, not a YAML re-serialize) |
| Merge person | `move "<old>" "<existing>"` (target exists) | ⚠️ **Wrap in Python.** Clobbers the target file and duplicates the link (`people: [[Bob]], [[Bob]]`) with no dedup. Only call `move` when target is absent; otherwise do backlink rewrite + dedup + body-merge ourselves |
| Patch a property | `frontmatter "<note>" --edit --key … --value …` | ❌ **DO NOT USE.** Three corruptions: (1) no list-append — replaces the entire list; (2) mangles wikilinks: `[[Carol]]`→`[Carol]` (YAML reads `[[ ]]` as a nested flow seq); (3) stringifies scalars: `duration_minutes: 12`→`"12"`. Note bodies survive, but frontmatter is fully re-serialized (keys reordered, quotes/indent changed) |
| Delete | `delete "<note>"` | ✅ Use |

**`move` (rename) and `search-content` are the headline wins.** `frontmatter --edit` is
out — **Python owns all frontmatter writes** (ruamel.yaml, per project YAML conventions).

### 5.2 Python wrapper

New module `services/memory/vault_cli.py` — a thin, typed subprocess wrapper:

```python
class VaultCLI:
    def __init__(self, binary: str, vault_path: Path): ...
    def create(self, note: str, content: str = "", *, append=False, overwrite=False) -> Path
    def print(self, note: str, mentions: bool = False) -> str
    def list(self, path: str = "") -> list[str]
    def search_content(self, query: str, *, page=None, page_size=25) -> list[VaultMatch]
    def rename(self, old: str, new: str) -> None    # move; ONLY when `new` does not exist
    def delete(self, note: str) -> None
    # NOTE: no frontmatter methods — the CLI corrupts wikilinks/lists/scalars (see §5.1).
    #       Frontmatter reads/writes go through a Python helper (ruamel.yaml).
```

Frontmatter helper (separate, Python): read note → split frontmatter → mutate with
ruamel.yaml (preserves quotes/types, appends to `people:` list with dedup) → rewrite.
This is the hot-path primitive for "attach person after speaker reprocess."

- One registered vault per user: `{DATA_DIR}/conversation_docs/{user_id}/` (matches
  today's layout). Register once via `add-vault` on first use, or operate with explicit
  `--vault` paths.
- Always non-interactive; parse `--format json`; raise on non-zero exit with stderr.
- Binary location: vendored/built into the backend image (Go build in Dockerfile) or a
  pinned release binary; path via `NOTESMD_CLI_BIN`.
- **Concurrency caveat:** `move` rewrites many files; serialize vault-mutating ops
  per-user (a per-user lock) to avoid racing the memory worker.
- **Frontmatter round-trip: RESOLVED (spike 2026-06-03)** — the CLI's `frontmatter --edit`
  corrupts our data (collapses `[[wikilinks]]`, overwrites lists, stringifies numbers). Do
  NOT use it. Python (ruamel.yaml) owns frontmatter. `move` is fine — it's a textual
  link-replace that does not touch unrelated frontmatter.

### 5.3 Grep-backed `search_memories()`

When `graph_storage_enabled=False`, `chronicle.search_memories()` becomes:

1. `VaultCLI.search_content(query)` → matches (file, line, snippet).
2. Group matches by note; for top-N notes, `print` the note (or the matched section).
3. Return `MemoryEntry` objects (same shape consumers already expect — `.content`, ids,
   metadata with `conversation_id`/source path). No interface change upstream.

This keeps `chat_service`, `memory_controller`, and the summary job working unchanged.

---

## 6. Workstream 3 — Kepano vault refinement + agentic retrieval

### 6.1 Note types & frontmatter (Kepano-mapped)

**Conversation note** `Conversations/<conv_id>.md` (evolve the existing doc — switch
`speakers:` strings to linked `people:`/`topics:` properties):

```yaml
---
categories: ["[[Conversations]]"]
conversation_id: <uuid>
date: <iso>
people: ["[[Alice]]", "[[Bob]]"]      # wikilink properties — queryable by Bases
topics: ["[[AI Safety]]"]
duration_minutes: <n>
---
## Title
### Summary
### Key Facts
### People
### Action Items
```

**Person note** `People/<name>.md` (created only when there is durable knowledge to
record; speaker identification alone does not mint a profile):

```yaml
---
categories: ["[[People]]"]
aliases: []          # other names / speaker labels seen
created: <iso>
updated: <iso>
# learned attributes appended over time: org, role, etc.
---
## Conversations
![[Conversations.base#Person]]   # auto-lists every conversation where people: contains this note
```

**Topic note** `Topics/<topic>.md` — same shape, `categories: ["[[Topics]]"]`.

### 6.2 `.base` files (the aggregation layer)

Ship into vault root (and a `Templates/Bases/` for editing). Minimum set:

- `People.base` — `categories.contains(link("People"))` → people with durable semantic
  profiles. The speaker enrollment/gallery API is the separate voice roster.
- `Conversations.base` with views:
  - `All` (sorted by date)
  - `Person` — filter `list(people).contains(this)` → embedded in each person note.
  - `Topic` — filter `list(topics).contains(this)`.
- `Topics.base`, and optionally `Related.base` / `Backlinks.base` (copy Kepano's).

Plus thin hub notes: `People.md = ![[People.base]]`, `Conversations.md`, `Topics.md`.

These are static templates we write once per user vault (or seed a shared `Templates/`).

### 6.3 People upsert in the memory job

The active transcript keeps every recognized speaker label, independently of the
vault. Conversation notes may link identified people, and Timeline episodes carry
recognized names as entities, without requiring a corresponding person note.

The memory agent creates `People/<name>.md` only when the source establishes durable
identity, relationship, work, preference, constraint, responsibility, or another
reusable fact. Routine appearances and a bare name remain in transcript/timeline
evidence; an unresolved wikilink is preferable to a placeholder profile. Speaker
reprocessing may rename or merge an existing real person note and its backlinks, but
must never create a note for `Unknown Speaker N`.

> Identity resolution (is "John" the same as "John Smith"?) is explicitly **out of scope
> for v1** — design the property/link/base layer so it *works* with whatever names exist,
> and bolt on merging later (a `move` is exactly the merge primitive).

### 6.4 Agentic, vault-aware retrieval (the "find info fast" vision)

Two layers:

**(a) Drop-in grep** (Workstream 2.3) — already covers `/api/memories/search` and the
"always" memory-injection chat mode.

**(b) Agentic vault navigation** for chat "tool" mode (`chat_service.py:534`): expose the
vault as a small tool set to the chat LLM —

- `vault_search(query)` → `search-content --format json`
- `vault_read(note, section?)` → `print`
- `vault_list(folder?)` → `list`
- `vault_person(name)` → `print People/<name>` (gets their conversation backlinks)

The LLM decides how to navigate (search → open person → read their conversations),
mirroring how a human uses Obsidian. This is where "a good system prompt about how the
vault is set up" pays off.

### 6.5 The vault-map system prompt (user-updatable / learnable)

Add a prompt `memory.vault_map` to `prompt_defaults.py`, retrieved via
`prompt_registry.get_prompt()` (so it inherits the existing **Langfuse override**
mechanism — that *is* the "let the user update the system prompt" path today). It
describes the live vault layout to the retrieval/chat LLM:

```
The user's knowledge is a markdown vault. Layout:
- People/<Name>.md      — one note per person; frontmatter categories:[[People]],
                          aliases:[...]; body lists their conversations.
- Conversations/<id>.md — frontmatter people:[[...]] topics:[[...]] date:...;
                          sections Summary / Key Facts / People / Action Items.
- Topics/<t>.md         — one per topic.
To find something: search content first; to learn about a person open People/<Name>.md
and read their linked conversations; key facts are verbatim — quote them, don't paraphrase.
```

Two levels of "updatable":

- **Manual:** user edits the prompt in Langfuse (or a future admin endpoint), e.g. to add
  vault conventions they care about.
- **Learned (later):** a periodic job summarizes the user's actual vault (folder counts,
  recurring property values, naming conventions, frequent topics) and writes that summary
  into a variable the `memory.vault_map` template interpolates — so the prompt *describes
  the user's real vault*, not a generic one. Start manual; design the template with a
  `{{vault_summary}}` slot so the learned version is a drop-in.

---

## 7. Rollout plan

| Phase | Deliverable | Risk | Status |
|---|---|---|---|
| 1 | FalkorDB benched behind a toggle (then **removed entirely** — FalkorDB, graph_client, embeddings, KG service, all `FALKORDB_*` env vars gone; vault is the sole store) | Low | ✅ Done (live) |
| 2 | Ripgrep-backed `search_memories()` (agentic, LLM-formulated regex; `rg` in image) | Med — replaced `notesmd-cli` grep with Claude-Code-style ripgrep | ✅ Done |
| 3 | Person/Topic notes + `people:`/`topics:` link props + `.base` files + hubs | Med — doc-generation moved to the tool-calling agent; identity left simple | ✅ Done |
| 4 | Tool-calling memory agent (write) + agentic vault search + `{{vault_summary}}` slot | Low — additive | ✅ Done |
| 5 | Learned `{{vault_summary}}` injection (periodic vault summariser) | Low — optional | ⏳ Parked |

Implementation notes (diverged from the original plan, all intentional):

- **Write path** is a bespoke tool-calling agent (`services/memory/agent/`), not `notesmd-cli`
  `create`/`frontmatter`. `add_memory` → `MemoryAgent.run` (provider-level, so all callers
  go through `MemoryServiceBase`). The agent greps the vault, writes the conversation note,
  and surgically `edit_note`s person notes (`edit_engine.py`, exact-then-fuzzy replace).
- **Search** is `search_vault()` — a read-only agent driving ripgrep (`grep`/`glob`/`read_note`);
  no query tokenization (the LLM formulates the regex).
- **`.base` + hubs** are seeded per-user by `vault_scaffold.seed_vault_scaffold()` (idempotent),
  called from the agent write/reprocess paths. Syntax (`categories.contains(link("People"))`,
  `list(people).contains(this)`, `![[Conversations.base#Person]]`) is copied verbatim from the
  reference Kepano vault's `People.base` / `Meetings.base` / People template.
- **Speaker reprocess** in agent mode (`_reprocess_memory_agent`) deletes only the conversation
  note and hands the agent the old→new speaker map as guidance so it calls `rename_person`
  (rewrites all backlinks) rather than orphaning `[[Speaker 0]]` notes.
- **Still parked:** per-user vault write lock for concurrent renames; Phase-5 learned
  `{{vault_summary}}`. (The old idea of projecting agent-extracted relationships back into
  FalkorDB is moot — FalkorDB has been removed entirely; the vault is the sole store.)

`notesmd-cli` is built into the backend image (Dockerfile `notesmd-builder` stage, pinned to
commit `cae9aa8`) and used for `rename_person` (its `move` renames the note + rewrites every
`[[wikilink]]` in one shot). Invoked with `--vault <abs path>` so no Obsidian config/registration
is needed (`Vault.Path()` returns an absolute name directly). A Python backlink-rewrite remains
as the fallback if the binary is ever absent, and the merge-onto-existing case stays in Python
(`move` clobbers an existing target — spike §5.1).

## 8. Open decisions

1. **Binary delivery** — build `notesmd-cli` from `untracked/` in the Dockerfile, or pin a
   released binary? (Leaning: multi-stage Go build in the backend image.)
2. **Conversation note filename** — keep `<conv_id>.md` (stable, opaque) or a human title
   (`YYYY-MM-DD <title>.md`, prettier in Obsidian but rename-prone)? `move` makes renames
   safe, so a title-based scheme is viable.
3. **Vault per user vs shared** — current layout is per-user subfolder; keep that and
   register each as its own notesmd-cli vault.
4. **Frontmatter YAML round-trip — RESOLVED (spike 2026-06-03).** CLI `frontmatter --edit`
   corrupts wikilinks/lists/scalars; rejected. Python (ruamel.yaml) owns frontmatter; CLI
   reserved for `create`/`rename`/`search`/`print`/`list`. Merge-onto-existing also handled
   in Python (`move` clobbers an existing target).
5. **Embeddings** — fully drop when graph is off (Phase 1), or keep generating for a future
   re-enable? (Leaning: drop — that's the speed win; FalkorDB can re-index from the vault
   later since the vault is ground truth.)
```

## 9. Update (2026-06-03) — Kepano templates + organic categories

Supersedes the flat-base scaffold in §6.2 and the inline note-format block in the agent
prompt. Reference: `untracked/kepano/stephango-vault.md` ("emergent structure"; "a template
for every category"; properties short and reusable).

**Layout — templates and bases in one place, Kepano-faithful** (`vault_scaffold.py`):
```
vault_root/
  People.md  Conversations.md  Topics.md      ← category hub notes (root)
  Templates/
    Person Template.md  Conversation Template.md  Topic Template.md
    Bases/  People.base  Conversations.base  Topics.base
```
Notes aggregate by the `categories` property, not by folder, so `Conversations/`/`People/`
folders are just tidiness. Hubs embed their base by basename (`![[People.base]]`), which
resolves even though the base now lives in `Templates/Bases/`. Note enumeration
(`is_scaffold_note`, used by `chronicle._vault_entries` + `vault_manager.list_docs`) skips
the whole `Templates/` subtree and the root hubs.

**Templates are the schema, single-sourced** (`vault_templates.py`): the three spine
templates live once and are (a) seeded as files into `Templates/`, (b) injected verbatim
into the memory-agent system prompt (with `{{date}}`/`{{title}}` rewritten to `<date>`/
`<title>` so the only mustache token left for LangFuse `compile()` is `{{vault_summary}}`).
This is what grounds the LLM — it fills a fixed field set instead of improvising structure.

**Organic categories** — the spine (People/Conversations/Topics) is the only thing seeded.
New *kinds* of things grow on demand: the agent's `create_category(name, properties)` tool
(`vault_tools.py` → `vault_scaffold.write_category`) idempotently stamps a
`Templates/<Cat> Template.md` + `Templates/Bases/<Cat>.base` + `<Cat>.md` hub, then files
the note under `<Cat>/<Name>.md`. The prompt encodes the Kepano conventions (pluralize,
reuse property names, link profusely, don't over-create).

**`notesmd-cli` fork** — `create --template <name>` added (resolves a template from the
`.obsidian/templates.json` folder, substitutes `{{title}}/{{date}}/{{time}}`). Lives on
`AnkushMalaker/notesmd-cli` branch `feat/create-template`; the backend Dockerfile is repinned
to `NOTESMD_REPO=AnkushMalaker/notesmd-cli` `NOTESMD_REF=c906cc10`. The official Obsidian CLI
has this but is closed-source, so the feature was replicated. The agent write path stays
Python (`write_note`/`edit_note`); `--template` is the standalone CLI capability.
