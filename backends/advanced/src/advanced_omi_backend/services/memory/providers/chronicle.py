"""Chronicle memory service — FalkorDB hybrid search + Markdown vault.

This module provides the core MemoryService class that:
- Generates rich conversation documents (.md) via LLM
- Stores them in an Obsidian-compatible vault (data/conversation_docs/)
- Indexes chunks in FalkorDB for hybrid search (vector + BM25 + recency bias)
- Links mentioned entities via ConvEntity nodes in the graph
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from ..base import MemoryEntry, MemoryServiceBase
from ..config import MemoryConfig
from ..graph_utils import compute_hybrid_scores, parse_conversation_doc
from ..vault_manager import ConvDocVaultManager
from .llm_providers import OpenAIProvider

memory_logger = logging.getLogger("memory_service")


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

        # FalkorDB — lazy-initialized like KnowledgeGraphService
        self._graph_client = None
        self._graph_read = None
        self._graph_write = None

    async def initialize(self) -> None:
        if self._initialized:
            return

        try:
            from advanced_omi_backend.services.graph_client import (
                GraphClient,
                GraphReadInterface,
                GraphWriteInterface,
            )

            # LLM provider (OpenAI-compatible — used for embeddings + doc generation)
            self.llm_provider = OpenAIProvider(self.config.llm_config)

            # FalkorDB connection (same env vars as KnowledgeGraphService)
            falkordb_host = os.getenv("FALKORDB_HOST", "falkordb")
            falkordb_port = int(os.getenv("FALKORDB_PORT", "6379"))

            self._graph_client = GraphClient(
                host=falkordb_host, port=falkordb_port, graph_name="chronicle"
            )
            self._graph_read = GraphReadInterface(self._graph_client)
            self._graph_write = GraphWriteInterface(self._graph_client)

            # Create schema (idempotent)
            await asyncio.to_thread(self._create_schema)

            # Test connections
            llm_ok = await self.llm_provider.test_connection()
            if not llm_ok:
                raise RuntimeError(
                    "LLM provider connection failed. "
                    "Check API keys, network connectivity, and service availability."
                )

            # Direct FalkorDB test (avoid calling self.test_connection which triggers initialize)
            graph_test = await asyncio.to_thread(
                self._graph_read.run, "RETURN 1 AS test"
            )
            if not graph_test or graph_test[0].get("test") != 1:
                raise RuntimeError(
                    "FalkorDB connection failed. Check that FalkorDB is running and accessible."
                )

            self._initialized = True
            memory_logger.info(
                "✅ Chronicle memory service initialized (FalkorDB + Vault). "
                "Existing Qdrant memories are not migrated."
            )

        except Exception as e:
            memory_logger.error(f"Memory service initialization failed: {e}")
            raise

    def _create_schema(self) -> None:
        """Create FalkorDB constraints and indexes (idempotent)."""
        from advanced_omi_backend.model_registry import get_models_registry

        reg = get_models_registry()
        embed_def = reg.get_default("embedding") if reg else None
        dims = (
            int(embed_def.embedding_dimensions)
            if embed_def and embed_def.embedding_dimensions
            else 1536
        )

        with self._graph_client.session() as session:
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

        memory_logger.info(
            "FalkorDB schema verified/created (ConvDoc/ConvChunk/ConvEntity)"
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
            doc_md = await self._generate_conversation_doc(
                transcript, source_id, user_id
            )

            # 2. Parse into typed structure (drops empty sections, extracts title/people)
            doc = parse_conversation_doc(doc_md)
            if not doc.sections:
                memory_logger.warning(
                    f"No meaningful sections from conversation doc for {source_id}"
                )
                return True, []

            # 3. Write to vault (ground truth)
            file_path = self.vault.write_doc(user_id, source_id, doc_md)

            # 4. Embed sections
            chunk_texts = [s.body for s in doc.sections]
            embeddings = await asyncio.wait_for(
                self.llm_provider.generate_embeddings(chunk_texts),
                timeout=self.config.timeout_seconds,
            )
            if not embeddings or len(embeddings) != len(doc.sections):
                raise RuntimeError(
                    f"Embedding generation failed for {source_id}: "
                    f"got {len(embeddings) if embeddings else 0} for {len(doc.sections)} sections"
                )

            # 5. Delete old data for this conversation (idempotent for first-time)
            await asyncio.to_thread(
                self._graph_write.run,
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
                source_id=source_id,
                user_id=user_id,
                doc=doc,
                embeddings=embeddings,
                file_path=str(file_path),
            )

            memory_logger.info(
                f"✅ Stored {len(chunk_ids)} chunks for conversation {source_id}"
            )
            return True, chunk_ids

        except asyncio.TimeoutError as e:
            memory_logger.error(f"⏰ Memory processing timed out for {source_id}")
            raise e
        except Exception as e:
            memory_logger.error(f"❌ Add memory failed for {source_id}: {e}")
            raise e

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

        # Use the OpenAI factory for async client (same pattern as llm_providers.py)
        from advanced_omi_backend.openai_factory import create_openai_client

        client = create_openai_client(
            api_key=self.llm_provider.api_key,
            base_url=self.llm_provider.base_url,
            is_async=True,
        )
        model = self.llm_provider.model

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Transcript:\n{transcript}"},
            ],
            temperature=0.2,
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

        with self._graph_client.session() as session:
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

                session.run(
                    """
                    MATCH (d:ConvDoc {conversation_id: $conv_id})
                    CREATE (c:ConvChunk {
                        id: $chunk_id,
                        text: $text,
                        section_title: $section_title,
                        embedding: $embedding,
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

        try:
            # Embed query
            query_embeddings = await self.llm_provider.generate_embeddings([query])
            if not query_embeddings or not query_embeddings[0]:
                memory_logger.error("Failed to generate query embedding")
                return []

            # Vector search
            vector_results = await asyncio.to_thread(
                self._vector_search,
                query_embeddings[0],
                user_id,
                limit * 2,
            )

            # Full-text search
            fulltext_results = await asyncio.to_thread(
                self._fulltext_search,
                query,
                user_id,
                limit * 2,
            )

            # Hybrid scoring
            scored = compute_hybrid_scores(vector_results, fulltext_results)

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

    def _vector_search(
        self, embedding: List[float], user_id: str, limit: int
    ) -> List[dict]:
        """Run vector similarity search in FalkorDB."""
        data = self._graph_read.run(
            """
            CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($embedding))
            YIELD node, score
            WHERE node.user_id = $user_id
            RETURN node.id AS chunk_id,
                   node.text AS text,
                   node.section_title AS section_title,
                   node.conversation_id AS conversation_id,
                   node.created_at AS date,
                   node.created_at AS created_at,
                   score
            """,
            embedding=embedding,
            user_id=user_id,
            limit=limit,
        )
        return data

    def _fulltext_search(self, query_text: str, user_id: str, limit: int) -> List[dict]:
        """Run full-text BM25 search in FalkorDB."""
        data = self._graph_read.run(
            """
            CALL db.idx.fulltext.queryNodes('ConvChunk', $search_text)
            YIELD node, score
            WHERE node.user_id = $user_id
            RETURN node.id AS chunk_id,
                   node.text AS text,
                   node.section_title AS section_title,
                   node.conversation_id AS conversation_id,
                   node.created_at AS date,
                   node.created_at AS created_at,
                   score
            LIMIT $limit
            """,
            search_text=query_text,
            user_id=user_id,
            limit=limit,
        )
        return data

    # =========================================================================
    # CRUD
    # =========================================================================

    async def get_all_memories(
        self, user_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        if not self._initialized:
            await self.initialize()

        try:
            data = await asyncio.to_thread(
                self._graph_read.run,
                """
                MATCH (c:ConvChunk {user_id: $user_id})
                RETURN c.id AS id, c.text AS text, c.section_title AS section_title,
                       c.conversation_id AS conversation_id, c.created_at AS created_at
                ORDER BY c.created_at DESC
                LIMIT $limit
                """,
                user_id=user_id,
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

        try:
            data = await asyncio.to_thread(
                self._graph_read.run,
                "MATCH (c:ConvChunk {user_id: $uid}) RETURN count(c) AS cnt",
                uid=user_id,
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

        try:
            query = "MATCH (c:ConvChunk {id: $id}) "
            params: dict = {"id": memory_id}
            if user_id:
                query += "WHERE c.user_id = $user_id "
                params["user_id"] = user_id
            query += (
                "RETURN c.id AS id, c.text AS text, c.section_title AS section_title, "
                "c.conversation_id AS conversation_id, c.created_at AS created_at, c.user_id AS user_id"
            )

            data = await asyncio.to_thread(self._graph_read.run, query, **params)
            if not data:
                return None

            row = data[0]
            return MemoryEntry(
                id=row["id"],
                content=row["text"],
                metadata={
                    "user_id": row.get("user_id", ""),
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
            data = await asyncio.to_thread(
                self._graph_read.run,
                """
                MATCH (d:ConvDoc {conversation_id: $source_id})-[:HAS_CHUNK]->(c:ConvChunk)
                WHERE c.user_id = $user_id
                RETURN c.id AS id, c.text AS text, c.section_title AS section_title,
                       c.conversation_id AS conversation_id, c.created_at AS created_at
                ORDER BY c.id
                LIMIT $limit
                """,
                source_id=source_id,
                user_id=user_id,
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

        try:
            data = await asyncio.to_thread(
                self._graph_write.run,
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

        try:
            # Delete FalkorDB nodes
            data = await asyncio.to_thread(
                self._graph_write.run,
                """
                MATCH (n)
                WHERE (n:ConvDoc OR n:ConvChunk OR n:ConvEntity) AND n.user_id = $uid
                DETACH DELETE n
                RETURN count(n) AS cnt
                """,
                uid=user_id,
            )
            graph_count = data[0]["cnt"] if data else 0

            # Delete vault files
            vault_count = self.vault.delete_all_docs(user_id)

            memory_logger.info(
                f"🗑️ Deleted {graph_count} FalkorDB nodes and {vault_count} vault files "
                f"for user {user_id}"
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

        try:
            # Delete old ConvDoc, chunks (including orphans), and vault file
            await asyncio.to_thread(
                self._graph_write.run,
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
        try:
            if not self._initialized:
                await self.initialize()
            data = await asyncio.to_thread(self._graph_read.run, "RETURN 1 AS test")
            return bool(data and data[0]["test"] == 1)
        except Exception as e:
            memory_logger.error(f"FalkorDB connection test failed: {e}")
            return False

    def shutdown(self) -> None:
        self._initialized = False
        self.llm_provider = None
        if self._graph_client:
            self._graph_client.close()
            self._graph_client = None
            self._graph_read = None
            self._graph_write = None
        memory_logger.info("Memory service shut down")
