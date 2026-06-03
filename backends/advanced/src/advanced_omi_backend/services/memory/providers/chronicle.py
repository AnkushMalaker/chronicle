"""Chronicle memory service — FalkorDB hybrid search + Markdown vault.

This module provides the core MemoryService class that:
- Generates rich conversation documents (.md) via LLM
- Stores them in an Obsidian-compatible vault (data/conversation_docs/)
- Indexes chunks in FalkorDB for hybrid search (vector + BM25 + recency bias)
- Links mentioned entities via ConvEntity nodes in the graph

Each user's data lives in its own per-user FalkorDB graph
(``chronicle_<user_id>``), which is the same graph KnowledgeGraphService and
ObsidianService use. Per-user graphs give us: (a) zero retrieval contamination
between users, (b) drop-the-graph delete in O(1) (no per-label DETACH sweeps),
and (c) per-user inspection (``GRAPH.QUERY chronicle_<user_id> "..."``).
"""

import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ..base import MemoryEntry, MemoryServiceBase
from ..config import MemoryConfig
from ..graph_utils import Section, compute_hybrid_scores, parse_conversation_doc
from ..vault_manager import ConvDocVaultManager
from ..vault_scaffold import SCAFFOLD_NOTE_NAMES, seed_vault_scaffold
from .llm_providers import OpenAIProvider

memory_logger = logging.getLogger("memory_service")


def _agent_mode_enabled() -> bool:
    """Vault-first agent mode: write via the memory agent, search via vault grep,
    and skip FalkorDB entirely. Toggled with ``MEMORY_AGENT_ENABLED``."""
    return os.getenv("MEMORY_AGENT_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


# Sections we expand into one chunk per bullet so per-fact embedding signal
# isn't diluted by stacking N unrelated facts in a single chunk. The Key Facts
# section often holds 5–15 separate facts; collapsing them to one chunk makes
# vector + BM25 underrank the right fact (LongMemEval audit confirmed this:
# ConvDoc contained "Yellow dress" verbatim but ranked below top-30 because
# the chunk also held 7 other gifts + restaurant menus).
_BULLET_EXPAND_SECTIONS = {"key facts"}


_BULLET_RE = re.compile(r"^(\s*)([-*])\s+(.*)$")


def _split_section_into_bullets(section: Section) -> List[Section]:
    """Split a section's body into one Section per leaf bullet.

    A "leaf bullet" is a bullet line that has no nested children. For nested
    structures we emit one chunk per leaf with its ancestor headers prepended
    so the chunk stands alone in retrieval. Non-bullet preamble (e.g.
    paragraph text before the list) becomes its own chunk.

    Example::

        - Gifts for the following occasions were discussed:
          - Sister's birthday: Yellow dress.
          - Gift for Mom: Silver hoop earrings.

    becomes two chunks::

        Gifts for the following occasions were discussed:
        - Sister's birthday: Yellow dress.

        Gifts for the following occasions were discussed:
        - Gift for Mom: Silver hoop earrings.

    If the body has no bullets the original section is returned unchanged.
    """
    lines = section.body.split("\n")
    parsed: list[tuple[int, str] | tuple[None, str]] = []
    for raw in lines:
        m = _BULLET_RE.match(raw)
        if m:
            indent = len(m.group(1))
            text = m.group(3).rstrip()
            parsed.append((indent, text))
        else:
            parsed.append((None, raw))

    if not any(p[0] is not None for p in parsed):
        return [section]

    out: list[Section] = []
    preamble_lines: list[str] = []
    for p in parsed:
        if p[0] is None and not preamble_lines and not out and not p[1].strip() == "":
            # leading non-bullet text up to first bullet
            preamble_lines.append(p[1])
        else:
            break
    if any(s.strip() for s in preamble_lines):
        out.append(Section(title=section.title, body="\n".join(preamble_lines).strip()))

    # Walk parsed entries, tracking the ancestor stack of bullet headers.
    # A bullet is "internal" if the next bullet has a strictly greater indent;
    # otherwise it's a leaf.
    bullet_indices = [i for i, p in enumerate(parsed) if p[0] is not None]
    for k, i in enumerate(bullet_indices):
        indent_i, text_i = parsed[i]  # type: ignore[misc]
        # next bullet index (if any)
        next_idx = bullet_indices[k + 1] if k + 1 < len(bullet_indices) else None
        is_internal = (
            next_idx is not None
            and parsed[next_idx][0] is not None
            and parsed[next_idx][0] > indent_i  # type: ignore[operator]
        )
        if is_internal:
            continue  # skip — its children will carry it as a header
        # Build the ancestor path: walk backwards from i, collecting bullets
        # with strictly smaller indent (one per indent level).
        ancestors: list[str] = []
        last_indent = indent_i
        for j in range(k - 1, -1, -1):
            ind_j, txt_j = parsed[bullet_indices[j]]  # type: ignore[misc]
            if ind_j < last_indent:  # type: ignore[operator]
                ancestors.append(txt_j)
                last_indent = ind_j  # type: ignore[assignment]
                if ind_j == 0:
                    break
        ancestors.reverse()
        body_lines = [f"- {a}" for a in ancestors] + [f"- {text_i}"]
        out.append(Section(title=section.title, body="\n".join(body_lines)))

    return out if out else [section]


def _expand_bullet_sections(sections: List[Section]) -> List[Section]:
    """Apply bullet-splitting to sections whose title is in _BULLET_EXPAND_SECTIONS."""
    expanded: List[Section] = []
    for s in sections:
        if s.title.strip().lower() in _BULLET_EXPAND_SECTIONS:
            expanded.extend(_split_section_into_bullets(s))
        else:
            expanded.append(s)
    return expanded


class MemoryService(MemoryServiceBase):
    """Memory service backed by FalkorDB (search index) + Markdown vault (ground truth).

    Each conversation produces a structured .md document. The document is
    split on ### headers into chunks, embedded, and stored as ConvChunk
    nodes in FalkorDB. Search combines vector similarity, BM25 full-text,
    and recency scoring.
    """

    @property
    def provider_identifier(self) -> str:
        return "chronicle"

    def __init__(self, config: MemoryConfig):
        super().__init__()
        self.config = config
        self.llm_provider: Optional[OpenAIProvider] = None
        self.vault = ConvDocVaultManager()
        # Vault-first agent mode: add_memory runs the memory agent, search greps the
        # vault, and FalkorDB/embeddings are skipped entirely.
        self._agent_mode = _agent_mode_enabled()

        # FalkorDB connection params (resolved on initialize)
        self._falkordb_host: Optional[str] = None
        self._falkordb_port: Optional[int] = None

        # Per-user graph cache: user_id -> (client, read, write)
        # Schema is created lazily on first access to a given user's graph.
        # ``_io_lock`` guards the cache itself so we don't double-create the
        # schema if two coroutines race for the same user_id.
        self._io_cache: dict[str, tuple] = {}
        self._io_lock = threading.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return

        # Vault-first agent mode needs neither FalkorDB nor the embedding provider:
        # the agent generates + writes notes via its own tool-calling LLM, and search
        # greps the vault. Skip all FalkorDB/embedding setup.
        if self._agent_mode:
            self._initialized = True
            memory_logger.info(
                "✅ Chronicle memory service initialized (vault-first agent mode; "
                "FalkorDB + embeddings disabled)."
            )
            return

        try:
            # LLM provider (OpenAI-compatible — used for embeddings + doc generation)
            self.llm_provider = OpenAIProvider(self.config.llm_config)

            # FalkorDB connection params (same env vars as KnowledgeGraphService)
            self._falkordb_host = os.getenv("FALKORDB_HOST", "falkordb")
            self._falkordb_port = int(os.getenv("FALKORDB_PORT", "6379"))

            # Test LLM connection (FalkorDB is verified per-user on first access)
            llm_ok = await self.llm_provider.test_connection()
            if not llm_ok:
                raise RuntimeError(
                    "LLM provider connection failed. "
                    "Check API keys, network connectivity, and service availability."
                )

            # Bare FalkorDB ping via a probe graph (does not select_graph any
            # user namespace — just confirms the server responds). Use the
            # writable session because ``ro_query`` on an empty Redis key
            # raises "Invalid graph operation on empty key" — we don't write
            # anything, but the FalkorDB module routes empty graphs through
            # the writable codepath.
            from advanced_omi_backend.services.graph_client import GraphClient

            probe = GraphClient(
                host=self._falkordb_host,
                port=self._falkordb_port,
                graph_name="_chronicle_probe",
            )
            try:
                ping = await asyncio.to_thread(
                    lambda: probe.session().run("RETURN 1 AS test")
                )
                if not ping or ping[0].get("test") != 1:
                    raise RuntimeError(
                        "FalkorDB connection failed. Check that FalkorDB is running and accessible."
                    )
            finally:
                probe.close()

            self._initialized = True
            memory_logger.info(
                "✅ Chronicle memory service initialized (per-user FalkorDB graphs + Vault)."
            )

        except Exception as e:
            memory_logger.error(f"Memory service initialization failed: {e}")
            raise

    def _get_io(self, user_id: str) -> tuple:
        """Return ``(client, read, write)`` for ``user_id``'s graph.

        Lazy-creates the GraphClient and writes the schema on first access.
        Cached for the life of the service so subsequent calls for the same
        user reuse the connection.
        """
        from advanced_omi_backend.services.graph_client import (
            GraphClient,
            GraphReadInterface,
            GraphWriteInterface,
            graph_name_for_user,
        )

        # Fast path — no lock needed for hits
        cached = self._io_cache.get(user_id)
        if cached is not None:
            return cached

        with self._io_lock:
            cached = self._io_cache.get(user_id)
            if cached is not None:
                return cached

            client = GraphClient(
                host=self._falkordb_host,
                port=self._falkordb_port,
                graph_name=graph_name_for_user(user_id),
            )
            read = GraphReadInterface(client)
            write = GraphWriteInterface(client)
            self._create_schema(client)
            io = (client, read, write)
            self._io_cache[user_id] = io
            return io

    def _create_schema(self, client) -> None:
        """Create FalkorDB constraints and indexes on ``client``'s graph (idempotent)."""
        from advanced_omi_backend.model_registry import get_models_registry

        reg = get_models_registry()
        embed_def = reg.get_default("embedding") if reg else None
        dims = (
            int(embed_def.embedding_dimensions)
            if embed_def and embed_def.embedding_dimensions
            else 1536
        )

        with client.session() as session:
            # Bootstrap: a brand-new per-user graph has no Redis key yet, and
            # FalkorDB raises "Invalid graph operation on empty key" on
            # constraint/index commands until the key exists. A no-op MERGE
            # establishes the key cheaply (the marker node is harmless and
            # ignored by all real queries).
            try:
                session.run("MERGE (:_GraphInit {id: 1})")
            except Exception:
                pass
            try:
                session.run(
                    "CREATE CONSTRAINT FOR (d:ConvDoc) REQUIRE d.conversation_id IS UNIQUE"
                )
            except Exception:
                pass  # Already exists
            try:
                session.run(
                    "CREATE CONSTRAINT FOR (c:ConvChunk) REQUIRE c.id IS UNIQUE"
                )
            except Exception:
                pass  # Already exists
            try:
                session.run(
                    "CREATE CONSTRAINT FOR (e:ConvEntity) REQUIRE e.id IS UNIQUE"
                )
            except Exception:
                pass  # Already exists
            try:
                session.run(
                    f"""
                    CREATE VECTOR INDEX FOR (c:ConvChunk) ON (c.embedding)
                    OPTIONS {{dimension: {dims}, similarityFunction: 'cosine'}}
                    """
                )
            except Exception:
                pass  # Already exists
            try:
                session.run(
                    "CALL db.idx.fulltext.createNodeIndex('ConvChunk', 'text', 'section_title')"
                )
            except Exception:
                pass  # Already exists

        memory_logger.debug(
            "FalkorDB schema verified/created on graph %s", client.graph_name
        )

    # =========================================================================
    # ADD MEMORY
    # =========================================================================

    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None,
    ) -> Tuple[bool, List[str]]:
        await self._ensure_initialized()

        if self._agent_mode:
            return await self._add_memory_agent(transcript, source_id, user_id)

        t_start = time.perf_counter()

        try:
            if not transcript or len(transcript.strip()) < 10:
                memory_logger.info(f"Skipping empty transcript for {source_id}")
                return True, []

            if allow_update:
                memory_logger.debug(
                    f"allow_update=True ignored for {source_id} — "
                    "each conversation is a new document"
                )

            # 1. Generate conversation doc via LLM
            t0 = time.perf_counter()
            doc_md = await self._generate_conversation_doc(
                transcript, source_id, user_id
            )
            t_doc_llm = time.perf_counter() - t0

            # 2. Parse into typed structure (drops empty sections, extracts title/people)
            doc = parse_conversation_doc(doc_md)
            if not doc.sections:
                memory_logger.warning(
                    f"No meaningful sections from conversation doc for {source_id}"
                )
                return True, []

            # 2b. Expand bullet-list sections (e.g. Key Facts) into one chunk
            # per bullet so each fact has its own embedding + BM25 entry.
            doc.sections = _expand_bullet_sections(doc.sections)

            # 3. Write to vault (ground truth)
            t0 = time.perf_counter()
            file_path = self.vault.write_doc(user_id, source_id, doc_md)
            t_vault = time.perf_counter() - t0

            # 4. Embed sections
            t0 = time.perf_counter()
            chunk_texts = [s.body for s in doc.sections]
            embeddings = await asyncio.wait_for(
                self.llm_provider.generate_embeddings(chunk_texts),
                timeout=self.config.timeout_seconds,
            )
            t_embed = time.perf_counter() - t0
            if not embeddings or len(embeddings) != len(doc.sections):
                raise RuntimeError(
                    f"Embedding generation failed for {source_id}: "
                    f"got {len(embeddings) if embeddings else 0} for {len(doc.sections)} sections"
                )

            # 5. Delete old data for this conversation (idempotent for first-time)
            t0 = time.perf_counter()
            client, _, write = self._get_io(user_id)
            await asyncio.to_thread(
                write.run,
                """
                OPTIONAL MATCH (d:ConvDoc {conversation_id: $source_id})
                OPTIONAL MATCH (c:ConvChunk {conversation_id: $source_id})
                DETACH DELETE d, c
                """,
                source_id=source_id,
            )

            # 6. Store in FalkorDB
            chunk_ids = await asyncio.to_thread(
                self._store_in_graph,
                client=client,
                source_id=source_id,
                user_id=user_id,
                doc=doc,
                embeddings=embeddings,
                file_path=str(file_path),
            )
            t_graph = time.perf_counter() - t0

            t_total = time.perf_counter() - t_start
            memory_logger.info(
                "✅ add_memory %s: chunks=%d  doc_llm=%.2fs embed=%.2fs vault=%.3fs graph=%.3fs total=%.2fs",
                source_id,
                len(chunk_ids),
                t_doc_llm,
                t_embed,
                t_vault,
                t_graph,
                t_total,
            )
            return True, chunk_ids

        except asyncio.TimeoutError as e:
            memory_logger.error(f"⏰ Memory processing timed out for {source_id}")
            raise e
        except Exception as e:
            memory_logger.error(f"❌ Add memory failed for {source_id}: {e}")
            raise e

    # ------------------------------------------------------------------
    # Vault-first agent mode (MEMORY_AGENT_ENABLED)
    # ------------------------------------------------------------------

    async def _add_memory_agent(
        self, transcript: str, source_id: str, user_id: str
    ) -> Tuple[bool, List[str]]:
        """Write path via the tool-calling memory agent.

        The agent creates the conversation note and surgically edits person/topic
        notes in the user's vault. Returns ``(success, touched_paths)`` — the touched
        vault-relative note paths stand in for the chunk/memory ids the FalkorDB path
        returns, so the existing job bookkeeping (counts, versions) works unchanged.
        """
        from ..agent import MemoryAgent

        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info(f"Skipping empty transcript for {source_id}")
            return True, []

        t0 = time.perf_counter()
        user_root = self.vault.user_root(user_id)
        seed_vault_scaffold(user_root)  # idempotent: .base + hub notes
        agent = MemoryAgent(user_root)
        result = await agent.run(transcript, source_id)
        memory_logger.info(
            "✅ add_memory(agent) %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs) — %s",
            source_id,
            len(result.touched),
            result.rounds,
            result.tool_calls,
            len(result.errors),
            time.perf_counter() - t0,
            result.summary[:160],
        )
        return True, result.touched

    async def _reprocess_memory_agent(
        self,
        transcript: str,
        source_id: str,
        user_id: str,
        transcript_diff: Optional[list],
    ) -> Tuple[bool, List[str]]:
        """Reprocess path in agent mode.

        Deletes the stale conversation note so the agent re-records it cleanly, then runs
        the agent. When the reprocess came from speaker re-identification we hand the agent
        the old→new speaker map as guidance so it can ``rename_person`` (which rewrites all
        backlinks) instead of leaving orphaned ``[[Speaker 0]]`` notes. Person/topic notes
        are kept and surgically updated — only the conversation note is regenerated.
        """
        from ..agent import MemoryAgent

        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info(f"Skipping empty transcript for {source_id}")
            return True, []

        user_root = self.vault.user_root(user_id)
        seed_vault_scaffold(user_root)

        # Remove the old conversation note (agent writes Conversations/<id>.md and
        # write_note refuses to clobber). Person/topic notes are intentionally preserved.
        conv_note = user_root / "Conversations" / f"{Path(source_id).name}.md"
        if conv_note.exists():
            conv_note.unlink()

        guidance = self._speaker_rename_guidance(transcript_diff)

        t0 = time.perf_counter()
        agent = MemoryAgent(user_root)
        result = await agent.run(transcript, source_id, guidance=guidance)
        memory_logger.info(
            "✅ reprocess_memory(agent) %s: touched=%d rounds=%d tools=%d errors=%d (%.2fs) — %s",
            source_id,
            len(result.touched),
            result.rounds,
            result.tool_calls,
            len(result.errors),
            time.perf_counter() - t0,
            result.summary[:160],
        )
        return True, result.touched

    @staticmethod
    def _speaker_rename_guidance(transcript_diff: Optional[list]) -> str:
        """Turn a speaker diff into an instruction to rename the matching person notes."""
        if not transcript_diff:
            return ""
        renames: dict[str, str] = {}
        for ch in transcript_diff:
            if isinstance(ch, dict) and ch.get("type") == "speaker_change":
                old, new = ch.get("old_speaker"), ch.get("new_speaker")
                if old and new and old != new:
                    renames[old] = new
        if not renames:
            return ""
        pairs = "; ".join(f"'{o}' is now '{n}'" for o, n in renames.items())
        return (
            "This is a REPROCESS after speaker re-identification. Speaker labels changed: "
            f"{pairs}. For each change, if a People/<old name>.md note exists, call "
            "rename_person(old, new) FIRST — it renames the note and rewrites every "
            "[[wikilink]] across the vault — then record the conversation and update the "
            "renamed person notes. Do not leave notes under the old speaker labels."
        )

    async def _search_vault_grep(
        self, query: str, user_id: str, limit: int
    ) -> List[MemoryEntry]:
        """Read path: a read-only retrieval agent drives ripgrep over the vault.

        Modelled on Claude Code — the LLM formulates ripgrep patterns (no query
        preprocessing), reads the relevant notes, and synthesises an answer. We return
        one MemoryEntry per note the agent read (capped), with the synthesised answer
        as the top entry so chat gets both the conclusion and the supporting notes.
        """
        from ..agent import search_vault

        result = await search_vault(query, self.vault.user_root(user_id))

        results: List[MemoryEntry] = []
        if result.answer:
            results.append(
                MemoryEntry(
                    id=f"search:{user_id}",
                    content=result.answer,
                    metadata={"user_id": user_id, "kind": "vault_search_answer"},
                    score=1.0,
                    created_at=None,
                )
            )
        for note in result.notes[: max(0, limit - len(results))]:
            path = note["path"]
            conv_id = ""
            if path.startswith("Conversations/"):
                conv_id = path.split("/", 1)[1].rsplit(".md", 1)[0]
            results.append(
                MemoryEntry(
                    id=path,
                    content=note["content"][:1500],
                    metadata={
                        "user_id": user_id,
                        "note": path,
                        "conversation_id": conv_id,
                        "kind": "vault_note",
                    },
                    score=0.9,
                    created_at=None,
                )
            )
        memory_logger.info(
            f"🔍 vault search: '{query}' -> {len(result.notes)} note(s) read, "
            f"{result.rounds} round(s) (user: {user_id})"
        )
        return results[:limit]

    async def _generate_conversation_doc(
        self, transcript: str, source_id: str, user_id: str
    ) -> str:
        """Generate a structured markdown conversation document via LLM."""
        from advanced_omi_backend.prompt_registry import get_prompt_registry

        registry = get_prompt_registry()
        system_prompt = await registry.get_prompt(
            "memory.generate_conversation_doc",
            conversation_id=source_id,
            date=datetime.now(timezone.utc).isoformat(),
            speakers="see transcript",
            duration="unknown",
        )

        # Use the config-driven `memory_extraction` operation so temperature
        # and other params come from llm_operations in config.yml (UI-editable).
        # response_format is dropped because chronicle emits markdown sections,
        # not JSON.
        from advanced_omi_backend.model_registry import get_models_registry

        registry = get_models_registry()
        if not registry:
            raise RuntimeError("Model registry not initialized")
        op = registry.get_llm_operation("memory_extraction")
        api_params = op.to_api_params()
        api_params.pop("response_format", None)
        client = op.get_client(is_async=True)

        response = await client.chat.completions.create(
            **api_params,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Transcript:\n{transcript}"},
            ],
        )
        doc_md = response.choices[0].message.content.strip()

        # Fallback: if LLM returns non-markdown, store transcript as single chunk
        if not doc_md or "###" not in doc_md:
            memory_logger.warning(
                f"LLM returned non-markdown for {source_id}, using fallback"
            )
            doc_md = (
                f"---\nconversation_id: {source_id}\n"
                f"date: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
                f"## Conversation\n\n### Summary\n{transcript[:500]}\n"
            )

        return doc_md

    def _store_in_graph(
        self,
        client,
        source_id: str,
        user_id: str,
        doc: "ConversationDoc",
        embeddings: List[List[float]],
        file_path: str,
    ) -> List[str]:
        """Store conversation doc and sections in FalkorDB (sync, runs in thread)."""
        from ..graph_utils import ConversationDoc

        now_iso = datetime.now(timezone.utc).isoformat()
        chunk_ids = []

        # Extract summary from sections
        summary_text = ""
        for section in doc.sections:
            if section.title.lower() == "summary":
                summary_text = section.body
                break

        with client.session() as session:
            # CREATE ConvDoc node
            session.run(
                """
                CREATE (d:ConvDoc {
                    conversation_id: $conv_id,
                    title: $title,
                    summary: $summary,
                    date: $date,
                    user_id: $user_id,
                    file_path: $file_path,
                    updated_at: $now
                })
                """,
                conv_id=source_id,
                title=doc.title,
                summary=summary_text,
                date=doc.frontmatter.date or now_iso,
                user_id=user_id,
                file_path=file_path,
                now=now_iso,
            )

            # CREATE chunks
            for i, (section, embedding) in enumerate(zip(doc.sections, embeddings)):
                chunk_id = f"{source_id}_chunk_{i:03d}"
                chunk_ids.append(chunk_id)

                # vecf32($embedding) is required for FalkorDB's vector index
                # to pick up the property; storing a plain list/array makes the
                # index silently skip the node and queryNodes returns 0 hits.
                session.run(
                    """
                    MATCH (d:ConvDoc {conversation_id: $conv_id})
                    CREATE (c:ConvChunk {
                        id: $chunk_id,
                        text: $text,
                        section_title: $section_title,
                        embedding: vecf32($embedding),
                        user_id: $user_id,
                        conversation_id: $conv_id,
                        created_at: $now
                    })
                    CREATE (d)-[:HAS_CHUNK]->(c)
                    """,
                    chunk_id=chunk_id,
                    text=section.body,
                    section_title=section.title,
                    embedding=embedding,
                    user_id=user_id,
                    conv_id=source_id,
                    now=now_iso,
                )

            # CREATE ConvEntity nodes from parsed people
            for person in doc.people:
                entity_id = f"{user_id}_{person.name.lower().replace(' ', '_')}"
                session.run(
                    """
                    MERGE (e:ConvEntity {id: $entity_id})
                    SET e.name = $name,
                        e.description = $description,
                        e.user_id = $user_id
                    WITH e
                    MATCH (d:ConvDoc {conversation_id: $conv_id})
                    MERGE (d)-[:MENTIONS]->(e)
                    """,
                    entity_id=entity_id,
                    name=person.name,
                    description=person.description,
                    user_id=user_id,
                    conv_id=source_id,
                )

        return chunk_ids

    # =========================================================================
    # SEARCH
    # =========================================================================

    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        if self._agent_mode:
            return await self._search_vault_grep(query, user_id, limit)
        return await self._search_falkordb(query, user_id, limit, score_threshold)

    async def _search_falkordb(
        self, query: str, user_id: str, limit: int, score_threshold: float
    ) -> List[MemoryEntry]:
        """Hybrid FalkorDB search: vector + BM25 full-text + entity-graph BFS,
        combined by recency-biased hybrid scoring. The default (non-agent) read path."""
        try:
            _, read, _ = self._get_io(user_id)

            # Embed query
            query_embeddings = await self.llm_provider.generate_embeddings([query])
            if not query_embeddings or not query_embeddings[0]:
                memory_logger.error("Failed to generate query embedding")
                return []

            # Vector search
            vector_results = await asyncio.to_thread(
                self._vector_search,
                read,
                query_embeddings[0],
                limit * 2,
            )

            # Full-text search
            fulltext_results = await asyncio.to_thread(
                self._fulltext_search,
                read,
                query,
                limit * 2,
            )

            # Entity-graph BFS expansion: seed from top vector+BM25 hits, walk
            # one hop through shared :Entity nodes, pull in chunks neither
            # primary index surfaced. Pattern adapted from Graphiti's hybrid
            # search (see search/search.py:332-353 in the graphiti repo) — we
            # use entity-overlap count as the BFS-side score in lieu of their
            # node-distance reranker, since LongMemEval has no center node.
            bfs_results: List[dict] = []
            if self._entity_bfs_enabled():
                seed_ids = self._collect_bfs_seeds(
                    vector_results, fulltext_results, count=5
                )
                if seed_ids:
                    bfs_results = await asyncio.to_thread(
                        self._entity_bfs_search, read, seed_ids, limit
                    )

            # Hybrid scoring
            scored = compute_hybrid_scores(
                vector_results, fulltext_results, bfs_results=bfs_results
            )

            # Filter and limit
            results = []
            for entry in scored[:limit]:
                if entry["final_score"] < score_threshold:
                    continue
                results.append(
                    MemoryEntry(
                        id=entry["chunk_id"],
                        content=entry.get("text", ""),
                        metadata={
                            "user_id": user_id,
                            "section_title": entry.get("section_title", ""),
                            "conversation_id": entry.get("conversation_id", ""),
                            "date": entry.get("date", ""),
                        },
                        score=entry["final_score"],
                        created_at=entry.get("created_at"),
                    )
                )

            memory_logger.info(
                f"🔍 Found {len(results)} memories for query '{query}' (user: {user_id})"
            )
            return results

        except Exception as e:
            memory_logger.error(f"Search memories failed: {e}")
            return []

    @staticmethod
    def _vector_search(read, embedding: List[float], limit: int) -> List[dict]:
        """Run vector similarity search in the per-user FalkorDB graph."""
        data = read.run(
            """
            CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($embedding))
            YIELD node, score
            RETURN node.id AS chunk_id,
                   node.text AS text,
                   node.section_title AS section_title,
                   node.conversation_id AS conversation_id,
                   node.created_at AS date,
                   node.created_at AS created_at,
                   score
            """,
            embedding=embedding,
            limit=limit,
        )
        return data

    # RediSearch (and FalkorDB's fulltext via it) treats these as syntax tokens.
    # Leaving them in raw user text breaks the parser — e.g. a trailing "?" makes
    # the entire query return zero hits. Stripping to spaces lets the analyzer
    # tokenize the remaining words normally.
    _FULLTEXT_RESERVED = str.maketrans(
        {c: " " for c in ",.<>{}[]\"':;!?@#$%^&*()-+=~|\\/"}
    )

    @staticmethod
    def _sanitize_fulltext_query(text: str) -> str:
        cleaned = text.translate(MemoryService._FULLTEXT_RESERVED)
        return " ".join(cleaned.split())

    # RediSearch's built-in English stopword list normally drops articles,
    # prepositions, and a handful of common verbs at index AND query time —
    # but the OR operator (`a|b|c`) bypasses query-side stopword filtering
    # in RediSearch, so a token like "with" leaks through and matches every
    # chunk that contains it. We replicate the built-in list (per RediSearch
    # docs) and extend it with interrogatives, auxiliaries, and pronouns so
    # only content terms survive into the OR expression.
    _BM25_STOPWORDS = frozenset(
        {
            # RediSearch built-in English defaults
            "a",
            "is",
            "the",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "but",
            "by",
            "for",
            "if",
            "in",
            "into",
            "it",
            "no",
            "not",
            "of",
            "on",
            "or",
            "such",
            "that",
            "their",
            "then",
            "there",
            "these",
            "they",
            "this",
            "to",
            "was",
            "will",
            "with",
            # Extensions: interrogatives, auxiliaries, pronouns, common verbs
            "what",
            "where",
            "when",
            "who",
            "whom",
            "whose",
            "why",
            "how",
            "which",
            "do",
            "does",
            "did",
            "done",
            "doing",
            "were",
            "been",
            "being",
            "am",
            "have",
            "has",
            "had",
            "having",
            "can",
            "could",
            "should",
            "would",
            "shall",
            "may",
            "might",
            "must",
            "i",
            "me",
            "my",
            "mine",
            "myself",
            "you",
            "your",
            "yours",
            "yourself",
            "yourselves",
            "we",
            "us",
            "our",
            "ours",
            "ourselves",
            "he",
            "him",
            "his",
            "himself",
            "she",
            "her",
            "hers",
            "herself",
            "them",
            "theirs",
            "themselves",
            "its",
            "itself",
            "those",
            "from",
            "about",
            "against",
            "between",
            "through",
            "during",
            "before",
            "after",
            "above",
            "below",
            "up",
            "down",
            "out",
            "off",
            "over",
            "under",
            "again",
            "further",
            "once",
            "here",
            "now",
            "also",
            "than",
            "too",
            "very",
            "so",
            "just",
            "only",
            "any",
            "some",
            "all",
            "each",
            "every",
            "few",
            "more",
            "most",
            "other",
            "same",
            "own",
        }
    )

    # RediSearch defaults to AND across query terms, so a question like
    # "What degree did I graduate with?" requires every non-stopword token
    # to appear in the chunk — even though the answer chunk only contains
    # "degree" + "graduated". Joining with the OR operator lets BM25 rank
    # by how many content terms hit instead.
    @classmethod
    def _build_or_query(cls, query_text: str) -> str:
        sanitized = cls._sanitize_fulltext_query(query_text)
        if not sanitized:
            return ""
        terms = [t for t in sanitized.lower().split() if t not in cls._BM25_STOPWORDS]
        return "|".join(terms)

    @classmethod
    def _fulltext_search(cls, read, query_text: str, limit: int) -> List[dict]:
        """Run full-text BM25 search in the per-user FalkorDB graph."""
        or_query = cls._build_or_query(query_text)
        if not or_query:
            return []
        data = read.run(
            """
            CALL db.idx.fulltext.queryNodes('ConvChunk', $search_text)
            YIELD node, score
            RETURN node.id AS chunk_id,
                   node.text AS text,
                   node.section_title AS section_title,
                   node.conversation_id AS conversation_id,
                   node.created_at AS date,
                   node.created_at AS created_at,
                   score
            LIMIT $limit
            """,
            search_text=or_query,
            limit=limit,
        )
        return data

    @staticmethod
    def _entity_bfs_enabled() -> bool:
        """Env-gated so a benchmark can A/B with and without graph expansion."""
        return os.getenv("CHRONICLE_ENTITY_BFS", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    @staticmethod
    def _collect_bfs_seeds(
        vector_results: List[dict], fulltext_results: List[dict], count: int
    ) -> List[str]:
        """Top-N chunk_ids from each primary source, deduped, order-preserving."""
        seen: dict[str, None] = {}
        for source in (vector_results, fulltext_results):
            for r in source[:count]:
                cid = r.get("chunk_id")
                if cid and cid not in seen:
                    seen[cid] = None
        return list(seen.keys())

    @staticmethod
    def _entity_bfs_search(read, seed_chunk_ids: List[str], limit: int) -> List[dict]:
        """Walk seed chunks → shared entities → other chunks; rank by overlap.

        Returns chunks NOT in the seed set, ranked by how many entities they
        share with seeds. Returns an empty list if no entities are reachable
        (e.g. KG extraction was skipped or the data predates [:MENTIONS]).
        """
        if not seed_chunk_ids:
            return []
        data = read.run(
            """
            UNWIND $seed_ids AS sid
            MATCH (seed:ConvChunk {id: sid})-[:MENTIONS]->(e:Entity)
            WITH collect(DISTINCT e) AS entities, collect(DISTINCT sid) AS seed_set
            UNWIND entities AS entity
            MATCH (entity)<-[:MENTIONS]-(c2:ConvChunk)
            WHERE NOT c2.id IN seed_set
            WITH c2, count(DISTINCT entity) AS shared_entities
            RETURN c2.id AS chunk_id,
                   c2.text AS text,
                   c2.section_title AS section_title,
                   c2.conversation_id AS conversation_id,
                   c2.created_at AS date,
                   c2.created_at AS created_at,
                   shared_entities AS score
            ORDER BY shared_entities DESC
            LIMIT $limit
            """,
            seed_ids=seed_chunk_ids,
            limit=limit,
        )
        return data

    # =========================================================================
    # CRUD
    # =========================================================================

    def _vault_entry_from_path(
        self, user_id: str, path: Path, root: Path, content_limit: Optional[int] = None
    ) -> Optional[MemoryEntry]:
        """Build a MemoryEntry from one vault note (id = vault-relative path)."""
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        if content_limit is not None:
            content = content[:content_limit]
        rel = path.relative_to(root).as_posix()
        conv_id = (
            rel[len("Conversations/") : -3] if rel.startswith("Conversations/") else ""
        )
        return MemoryEntry(
            id=rel,
            content=content,
            metadata={
                "user_id": user_id,
                "note": rel,
                "conversation_id": conv_id,
                "kind": "vault_note",
            },
            created_at=None,
        )

    def _vault_entries(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[MemoryEntry]:
        """Enumerate a user's vault notes as MemoryEntry objects (newest first).

        The agent-mode equivalent of listing ConvChunk nodes — one entry per note,
        recursive over Conversations/People/Topics.
        """
        root = self.vault.user_root(user_id)
        if not root.exists():
            return []
        paths = sorted(
            (p for p in root.rglob("*.md") if p.name not in SCAFFOLD_NOTE_NAMES),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if limit is not None:
            paths = paths[:limit]
        entries: List[MemoryEntry] = []
        for p in paths:
            entry = self._vault_entry_from_path(user_id, p, root, content_limit=1500)
            if entry is not None:
                entries.append(entry)
        return entries

    async def get_all_memories(
        self, user_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        if self._agent_mode:
            return self._vault_entries(user_id, limit)

        try:
            _, read, _ = self._get_io(user_id)
            data = await asyncio.to_thread(
                read.run,
                """
                MATCH (c:ConvChunk)
                RETURN c.id AS id, c.text AS text, c.section_title AS section_title,
                       c.conversation_id AS conversation_id, c.created_at AS created_at
                ORDER BY c.created_at DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            memories = [
                MemoryEntry(
                    id=row["id"],
                    content=row["text"],
                    metadata={
                        "user_id": user_id,
                        "section_title": row.get("section_title", ""),
                        "conversation_id": row.get("conversation_id", ""),
                    },
                    created_at=row.get("created_at"),
                )
                for row in data
            ]
            memory_logger.info(
                f"📚 Retrieved {len(memories)} memories for user {user_id}"
            )
            return memories
        except Exception as e:
            memory_logger.error(f"Get all memories failed: {e}")
            return []

    async def count_memories(self, user_id: str) -> Optional[int]:
        if not self._initialized:
            await self.initialize()

        if self._agent_mode:
            return len(self.vault.list_docs(user_id))

        try:
            _, read, _ = self._get_io(user_id)
            data = await asyncio.to_thread(
                read.run,
                "MATCH (c:ConvChunk) RETURN count(c) AS cnt",
            )
            count = data[0]["cnt"] if data else 0
            memory_logger.info(f"🔢 Total {count} memories for user {user_id}")
            return count
        except Exception as e:
            memory_logger.error(f"Count memories failed: {e}")
            return None

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        if not user_id:
            # Per-user graphs require a user_id to know which graph to query.
            memory_logger.error(
                "get_memory called without user_id; per-user graphs require it"
            )
            return None

        try:
            _, read, _ = self._get_io(user_id)
            data = await asyncio.to_thread(
                read.run,
                """
                MATCH (c:ConvChunk {id: $id})
                RETURN c.id AS id, c.text AS text, c.section_title AS section_title,
                       c.conversation_id AS conversation_id, c.created_at AS created_at,
                       c.user_id AS user_id
                """,
                id=memory_id,
            )
            if not data:
                return None

            row = data[0]
            return MemoryEntry(
                id=row["id"],
                content=row["text"],
                metadata={
                    "user_id": row.get("user_id", user_id),
                    "section_title": row.get("section_title", ""),
                    "conversation_id": row.get("conversation_id", ""),
                },
                created_at=row.get("created_at"),
            )
        except Exception as e:
            memory_logger.error(f"Get memory failed: {e}")
            return None

    async def get_memories_by_source(
        self, user_id: str, source_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        try:
            _, read, _ = self._get_io(user_id)
            data = await asyncio.to_thread(
                read.run,
                """
                MATCH (d:ConvDoc {conversation_id: $source_id})-[:HAS_CHUNK]->(c:ConvChunk)
                RETURN c.id AS id, c.text AS text, c.section_title AS section_title,
                       c.conversation_id AS conversation_id, c.created_at AS created_at
                ORDER BY c.id
                LIMIT $limit
                """,
                source_id=source_id,
                limit=limit,
            )
            return [
                MemoryEntry(
                    id=row["id"],
                    content=row["text"],
                    metadata={
                        "user_id": user_id,
                        "section_title": row.get("section_title", ""),
                        "conversation_id": row.get("conversation_id", ""),
                    },
                    created_at=row.get("created_at"),
                )
                for row in data
            ]
        except Exception as e:
            memory_logger.error(f"Get memories by source failed: {e}")
            return []

    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        """Chunks are immutable document sections — update is not supported."""
        memory_logger.warning(
            f"update_memory called for {memory_id} but chunks are immutable. "
            "Use reprocess_memory to regenerate from transcript."
        )
        return False

    async def delete_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        if not self._initialized:
            await self.initialize()

        if not user_id:
            memory_logger.error(
                "delete_memory called without user_id; per-user graphs require it"
            )
            return False

        try:
            _, _, write = self._get_io(user_id)
            data = await asyncio.to_thread(
                write.run,
                "MATCH (c:ConvChunk {id: $id}) DETACH DELETE c RETURN count(c) AS cnt",
                id=memory_id,
            )
            deleted = data[0]["cnt"] > 0 if data else False
            if deleted:
                memory_logger.info(f"🗑️ Deleted memory {memory_id}")
            return deleted
        except Exception as e:
            memory_logger.error(f"Delete memory failed: {e}")
            return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        if not self._initialized:
            await self.initialize()

        if self._agent_mode:
            return self.vault.delete_all_docs(user_id)  # vault is the only store

        try:
            # Drop the entire per-user graph in one shot (O(1) on the server).
            # Also evicts the cached client/interfaces so the next access for
            # this user_id re-creates the graph + schema cleanly.
            client = None
            with self._io_lock:
                cached = self._io_cache.pop(user_id, None)
                if cached is not None:
                    client = cached[0]

            if client is None:
                # Not yet accessed in this process — open just to drop it.
                from advanced_omi_backend.services.graph_client import (
                    GraphClient,
                    graph_name_for_user,
                )

                client = GraphClient(
                    host=self._falkordb_host,
                    port=self._falkordb_port,
                    graph_name=graph_name_for_user(user_id),
                )

            graph_count = 0
            try:
                # Count first so we have a non-zero return for callers that
                # rely on it; cheap because the graph is small post-fix.
                # Writable session — ``ro_query`` on an empty Redis key fails
                # with "Invalid graph operation on empty key".
                count_rows = await asyncio.to_thread(
                    lambda: client.session().run(
                        "MATCH (n) WHERE n:ConvDoc OR n:ConvChunk OR n:ConvEntity "
                        "RETURN count(n) AS cnt"
                    )
                )
                graph_count = count_rows[0]["cnt"] if count_rows else 0
                await asyncio.to_thread(client.delete_graph)
            except Exception as e:
                # GRAPH.DELETE on a never-written graph raises; treat as no-op.
                memory_logger.debug(f"delete_graph for user {user_id} skipped: {e}")
            finally:
                client.close()

            # Delete vault files
            vault_count = self.vault.delete_all_docs(user_id)

            memory_logger.info(
                f"🗑️ Dropped per-user graph ({graph_count} memory nodes) and "
                f"{vault_count} vault files for user {user_id}"
            )
            return graph_count
        except Exception as e:
            memory_logger.error(f"Delete user memories failed: {e}")
            return 0

    async def reprocess_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        transcript_diff: Optional[list] = None,
        previous_transcript: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Delete old data and regenerate from transcript."""
        await self._ensure_initialized()

        if self._agent_mode:
            return await self._reprocess_memory_agent(
                transcript, source_id, user_id, transcript_diff
            )

        try:
            # Delete old ConvDoc, chunks (including orphans), and vault file
            _, _, write = self._get_io(user_id)
            await asyncio.to_thread(
                write.run,
                """
                OPTIONAL MATCH (d:ConvDoc {conversation_id: $source_id})
                OPTIONAL MATCH (c:ConvChunk {conversation_id: $source_id})
                DETACH DELETE d, c
                """,
                source_id=source_id,
            )
            self.vault.delete_doc(user_id, source_id)

            memory_logger.info(
                f"🔄 Reprocessing memory for {source_id} — deleted old data"
            )

            # Re-generate
            return await self.add_memory(
                transcript, client_id, source_id, user_id, user_email
            )

        except Exception as e:
            memory_logger.error(f"❌ Reprocess memory failed for {source_id}: {e}")
            return await self.add_memory(
                transcript, client_id, source_id, user_id, user_email
            )

    async def test_connection(self) -> bool:
        if self._agent_mode:
            return True  # vault-only: no FalkorDB to probe
        try:
            if not self._initialized:
                await self.initialize()
            from advanced_omi_backend.services.graph_client import GraphClient

            probe = GraphClient(
                host=self._falkordb_host,
                port=self._falkordb_port,
                graph_name="_chronicle_probe",
            )
            try:
                data = await asyncio.to_thread(
                    lambda: probe.session().run("RETURN 1 AS test")
                )
                return bool(data and data[0]["test"] == 1)
            finally:
                probe.close()
        except Exception as e:
            memory_logger.error(f"FalkorDB connection test failed: {e}")
            return False

    def shutdown(self) -> None:
        self._initialized = False
        self.llm_provider = None
        with self._io_lock:
            for client, _, _ in self._io_cache.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._io_cache.clear()
        memory_logger.info("Memory service shut down")
