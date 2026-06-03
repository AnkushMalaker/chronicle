"""Graphiti-backed memory provider.

This provider keeps Chronicle's ``MemoryServiceBase`` interface while delegating
memory extraction, graph storage, and retrieval to Graphiti. ``MemoryEntry`` is
only an API adapter here; the canonical data lives in Graphiti's FalkorDB graph.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from advanced_omi_backend.services.graph_client import (
    GraphClient,
    sanitize_user_id_for_graph,
)

from ..base import MemoryEntry, MemoryServiceBase
from ..config import MemoryConfig

memory_logger = logging.getLogger("memory_service")


_SOURCE_DATE_RE = re.compile(r"^\[(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]")

# Detects "Speaker_Label: " prefixes Chronicle uses for chat and audio
# transcripts (User/Assistant for chat, diarized speaker names for audio).
# Restricted to letters/digits/underscore/hyphen/space so prose like
# "Note:" or "* item:" inside an utterance does not split a turn.
_TURN_PREFIX_RE = re.compile(r"^([A-Za-z][\w \-]{0,40}): (.*)$")

_PERSONAL_MEMORY_INSTRUCTIONS = """
Extract durable personal-memory facts about the user and people they mention.
Prefer atomic, self-contained facts with explicit relation labels, temporal
qualifiers, and concrete values. Preserve changes over time rather than merging
current and previous states.
"""


@dataclass
class _GraphitiClasses:
    graphiti: Any
    falkor_driver: Any
    llm_config: Any
    openai_client: Any
    openai_embedder: Any
    openai_embedder_config: Any
    openai_reranker: Any
    episode_type: Any


class GraphitiMemoryService(MemoryServiceBase):
    """Memory service that stores conversations as Graphiti episodes and facts."""

    @property
    def provider_identifier(self) -> str:
        return "graphiti"

    def __init__(self, config: MemoryConfig):
        super().__init__()
        self.config = config
        self._classes: Optional[_GraphitiClasses] = None
        self._llm_config = None
        self._embedder_config = None
        self._falkordb_host: Optional[str] = None
        self._falkordb_port: Optional[int] = None
        self._falkordb_username: Optional[str] = None
        self._falkordb_password: Optional[str] = None
        self._max_coroutines: Optional[int] = None
        self._clients: dict[str, Any] = {}
        self._client_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return

        try:
            self._classes = self._load_graphiti_classes()
            llm_cfg = self.config.llm_config or {}

            self._falkordb_host = os.getenv("FALKORDB_HOST", "falkordb")
            self._falkordb_port = int(os.getenv("FALKORDB_PORT", "6379"))
            self._falkordb_username = os.getenv("FALKORDB_USERNAME") or None
            self._falkordb_password = os.getenv("FALKORDB_PASSWORD") or None
            self._max_coroutines = int(os.getenv("GRAPHITI_MAX_COROUTINES", "10"))

            self._llm_config = self._classes.llm_config(
                api_key=llm_cfg.get("api_key") or "",
                model=llm_cfg.get("model"),
                base_url=llm_cfg.get("base_url"),
                temperature=float(llm_cfg.get("temperature", 0.1)),
                max_tokens=int(llm_cfg.get("max_tokens", 2000)),
            )
            self._embedder_config = self._classes.openai_embedder_config(
                api_key=llm_cfg.get("api_key") or "",
                base_url=llm_cfg.get("base_url"),
                embedding_model=llm_cfg.get("embedding_model"),
                embedding_dim=self._embedding_dimensions(),
            )

            ok = await self.test_connection()
            if not ok:
                raise RuntimeError("Graphiti/FalkorDB connection test failed")

            self._initialized = True
            memory_logger.info("Graphiti memory service initialized")
        except Exception as exc:
            memory_logger.error(
                "Graphiti memory service initialization failed: %s", exc
            )
            raise

    def _load_graphiti_classes(self) -> _GraphitiClasses:
        try:
            from graphiti_core.cross_encoder.openai_reranker_client import (
                OpenAIRerankerClient,
            )
            from graphiti_core.driver.falkordb_driver import FalkorDriver
            from graphiti_core.embedder.openai import (
                OpenAIEmbedder,
                OpenAIEmbedderConfig,
            )
            from graphiti_core.graphiti import Graphiti
            from graphiti_core.llm_client.config import LLMConfig
            from graphiti_core.llm_client.openai_client import OpenAIClient
            from graphiti_core.nodes import EpisodeType
        except ImportError as exc:
            raise RuntimeError(
                "Graphiti provider requires graphiti_core to be importable. "
                "Install the local fork manually, for example: "
                "uv pip install -e ../../untracked/graphiti[falkordb]"
            ) from exc

        return _GraphitiClasses(
            graphiti=Graphiti,
            falkor_driver=FalkorDriver,
            llm_config=LLMConfig,
            openai_client=OpenAIClient,
            openai_embedder=OpenAIEmbedder,
            openai_embedder_config=OpenAIEmbedderConfig,
            openai_reranker=OpenAIRerankerClient,
            episode_type=EpisodeType,
        )

    def _embedding_dimensions(self) -> int:
        try:
            from advanced_omi_backend.model_registry import get_models_registry

            registry = get_models_registry()
            embed_def = registry.get_default("embedding") if registry else None
            if embed_def and embed_def.embedding_dimensions:
                return int(embed_def.embedding_dimensions)
        except Exception:
            memory_logger.debug("Falling back to default Graphiti embedding dimensions")

        return int(os.getenv("EMBEDDING_DIM", "1536"))

    async def _get_graphiti(self, user_id: str) -> tuple[Any, str]:
        await self._ensure_initialized()
        group_id = self._graphiti_group_id(user_id)
        cached = self._clients.get(group_id)
        if cached is not None:
            return cached, group_id

        async with self._client_lock:
            cached = self._clients.get(group_id)
            if cached is not None:
                return cached, group_id

            assert self._classes is not None
            driver = self._classes.falkor_driver(
                host=self._falkordb_host or "falkordb",
                port=self._falkordb_port or 6379,
                username=self._falkordb_username,
                password=self._falkordb_password,
                database=group_id,
            )
            graphiti = self._classes.graphiti(
                graph_driver=driver,
                llm_client=self._classes.openai_client(config=self._llm_config),
                embedder=self._classes.openai_embedder(config=self._embedder_config),
                cross_encoder=self._classes.openai_reranker(config=self._llm_config),
                max_coroutines=self._max_coroutines,
            )
            await graphiti.build_indices_and_constraints()
            self._clients[group_id] = graphiti
            return graphiti, group_id

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
        graphiti, group_id = await self._get_graphiti(user_id)

        if not transcript or len(transcript.strip()) < 10:
            memory_logger.info("Skipping empty transcript for %s", source_id)
            return True, []

        if allow_update:
            await self._delete_source_facts(graphiti, group_id, source_id)

        # Split multi-turn dialogue into one episode per turn. A single big
        # "User: ...\nAssistant: ...\nUser: ..." episode causes the LLM to
        # latch onto prominent topics and skip casual self-facts buried mid-
        # session (verified on LongMemEval QID 5d3d2817). Per-turn episodes
        # keep each utterance focused, and Graphiti pulls recent same-group
        # episodes back in as PREVIOUS_MESSAGES context for extraction.
        turns = self._split_into_turns(transcript)
        fallback_time = self._reference_time_from_transcript(transcript)
        source_description = (
            f"chronicle source_id={source_id}; client_id={client_id}; "
            f"user_id={user_id}; user_email={user_email}"
        )

        try:
            if len(turns) <= 1:
                result = await asyncio.wait_for(
                    graphiti.add_episode(
                        name=source_id,
                        episode_body=transcript,
                        source_description=source_description,
                        reference_time=fallback_time,
                        source=self._classes.episode_type.message,
                        group_id=group_id,
                        custom_extraction_instructions=_PERSONAL_MEMORY_INSTRUCTIONS,
                    ),
                    timeout=self.config.timeout_seconds,
                )
                memory_ids = [edge.uuid for edge in result.edges]
                memory_logger.info(
                    "Graphiti add_memory %s: episodes=1 facts=%d nodes=%d",
                    source_id,
                    len(memory_ids),
                    len(result.nodes),
                )
                return True, memory_ids

            memory_ids: list[str] = []
            total_nodes = 0
            for idx, turn in enumerate(turns):
                turn_time = self._reference_time_from_transcript(turn) or fallback_time
                result = await asyncio.wait_for(
                    graphiti.add_episode(
                        name=f"{source_id}#turn{idx}",
                        episode_body=turn,
                        source_description=source_description,
                        reference_time=turn_time,
                        source=self._classes.episode_type.message,
                        group_id=group_id,
                        custom_extraction_instructions=_PERSONAL_MEMORY_INSTRUCTIONS,
                    ),
                    timeout=self.config.timeout_seconds,
                )
                memory_ids.extend(edge.uuid for edge in result.edges)
                total_nodes += len(result.nodes)
            memory_logger.info(
                "Graphiti add_memory %s: episodes=%d facts=%d nodes=%d",
                source_id,
                len(turns),
                len(memory_ids),
                total_nodes,
            )
            return True, memory_ids
        except Exception as exc:
            memory_logger.error("Graphiti add_memory failed for %s: %s", source_id, exc)
            raise

    def _split_into_turns(self, transcript: str) -> list[str]:
        """Split a Chronicle dialogue transcript into one string per turn.

        Lines that match a `Speaker: text` prefix start new turns; lines that
        don't (continuation lines, blank lines, markdown bullets within a
        single utterance) are appended to the current turn. If fewer than two
        turn-prefixed lines are found, returns a single-element list so we
        fall back to single-episode ingestion.
        """
        lines = transcript.splitlines()
        turns: list[list[str]] = []
        for line in lines:
            if _TURN_PREFIX_RE.match(line):
                turns.append([line])
            elif turns:
                turns[-1].append(line)
            elif line.strip():
                turns.append([line])

        if len(turns) < 2:
            return [transcript]
        return ["\n".join(t).rstrip() for t in turns if any(s.strip() for s in t)]

    def _reference_time_from_transcript(self, transcript: str) -> datetime:
        first_line = transcript.lstrip().splitlines()[0] if transcript.strip() else ""
        match = _SOURCE_DATE_RE.search(first_line)
        if match:
            parsed = datetime.strptime(match.group("date"), "%Y-%m-%d %H:%M")
            return parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)

    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        graphiti, group_id = await self._get_graphiti(user_id)

        try:
            edges = await asyncio.wait_for(
                graphiti.search(query=query, group_ids=[group_id], num_results=limit),
                timeout=self.config.timeout_seconds,
            )
            memories = [self._edge_to_memory_entry(edge, user_id) for edge in edges]
            memory_logger.info(
                "Graphiti search found %d memories for query %r (user=%s)",
                len(memories),
                query,
                user_id,
            )
            return memories
        except Exception as exc:
            memory_logger.error("Graphiti search failed for user %s: %s", user_id, exc)
            return []

    async def get_all_memories(
        self, user_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        graphiti, group_id = await self._get_graphiti(user_id)
        rows = await self._query_edges(
            graphiti,
            """
            MATCH (:Entity)-[e:RELATES_TO]->(:Entity)
            WHERE e.group_id = $group_id
            RETURN e.uuid AS uuid, e.fact AS fact, e.name AS name,
                   e.episodes AS episodes, e.created_at AS created_at,
                   e.valid_at AS valid_at, e.invalid_at AS invalid_at,
                   e.expired_at AS expired_at, e.reference_time AS reference_time
            ORDER BY e.created_at DESC
            LIMIT $limit
            """,
            group_id=group_id,
            limit=limit,
        )
        return [self._row_to_memory_entry(row, user_id) for row in rows]

    async def count_memories(self, user_id: str) -> Optional[int]:
        graphiti, group_id = await self._get_graphiti(user_id)
        rows = await self._query_edges(
            graphiti,
            """
            MATCH (:Entity)-[e:RELATES_TO]->(:Entity)
            WHERE e.group_id = $group_id
            RETURN count(e) AS cnt
            """,
            group_id=group_id,
        )
        return int(rows[0]["cnt"]) if rows else 0

    async def get_memory(
        self, memory_id: str, user_id: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        if not user_id:
            memory_logger.error("Graphiti get_memory requires user_id")
            return None

        graphiti, group_id = await self._get_graphiti(user_id)
        rows = await self._query_edges(
            graphiti,
            """
            MATCH (:Entity)-[e:RELATES_TO {uuid: $memory_id}]->(:Entity)
            WHERE e.group_id = $group_id
            RETURN e.uuid AS uuid, e.fact AS fact, e.name AS name,
                   e.episodes AS episodes, e.created_at AS created_at,
                   e.valid_at AS valid_at, e.invalid_at AS invalid_at,
                   e.expired_at AS expired_at, e.reference_time AS reference_time
            LIMIT 1
            """,
            memory_id=memory_id,
            group_id=group_id,
        )
        return self._row_to_memory_entry(rows[0], user_id) if rows else None

    async def get_memories_by_source(
        self, user_id: str, source_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        graphiti, group_id = await self._get_graphiti(user_id)
        rows = await self._query_edges(
            graphiti,
            """
            MATCH (:Entity)-[e:RELATES_TO]->(:Entity)
            WHERE e.group_id = $group_id AND $source_id IN e.episodes
            RETURN e.uuid AS uuid, e.fact AS fact, e.name AS name,
                   e.episodes AS episodes, e.created_at AS created_at,
                   e.valid_at AS valid_at, e.invalid_at AS invalid_at,
                   e.expired_at AS expired_at, e.reference_time AS reference_time
            ORDER BY e.created_at DESC
            LIMIT $limit
            """,
            group_id=group_id,
            source_id=source_id,
            limit=limit,
        )
        return [self._row_to_memory_entry(row, user_id) for row in rows]

    async def delete_memory(
        self,
        memory_id: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> bool:
        if not user_id:
            memory_logger.error("Graphiti delete_memory requires user_id")
            return False

        graphiti, group_id = await self._get_graphiti(user_id)
        rows = await self._query_edges(
            graphiti,
            """
            MATCH (:Entity)-[e:RELATES_TO {uuid: $memory_id}]->(:Entity)
            WHERE e.group_id = $group_id
            WITH collect(e) AS edges
            FOREACH (edge IN edges | DELETE edge)
            RETURN size(edges) AS cnt
            """,
            memory_id=memory_id,
            group_id=group_id,
        )
        return bool(rows and rows[0].get("cnt", 0) > 0)

    async def delete_all_user_memories(self, user_id: str) -> int:
        await self._ensure_initialized()
        group_id = self._graphiti_group_id(user_id)
        graphiti = self._clients.pop(group_id, None)
        if graphiti is not None:
            try:
                await graphiti.close()
            except Exception as exc:
                memory_logger.debug("Graphiti close during delete skipped: %s", exc)

        count = await self._count_graph_nodes(group_id)
        client = GraphClient(
            host=self._falkordb_host or "falkordb",
            port=self._falkordb_port or 6379,
            graph_name=group_id,
        )
        try:
            await asyncio.to_thread(client.delete_graph)
        except Exception as exc:
            memory_logger.debug(
                "Graphiti graph delete for %s skipped: %s", user_id, exc
            )
        finally:
            client.close()

        return count

    async def _count_graph_nodes(self, graph_name: str) -> int:
        client = GraphClient(
            host=self._falkordb_host or "falkordb",
            port=self._falkordb_port or 6379,
            graph_name=graph_name,
        )
        try:
            rows = await asyncio.to_thread(
                lambda: client.session().run(
                    "MATCH (n) WHERE n:Entity OR n:Episodic OR n:Community "
                    "RETURN count(n) AS cnt"
                )
            )
            return int(rows[0]["cnt"]) if rows else 0
        except Exception:
            return 0
        finally:
            client.close()

    async def test_connection(self) -> bool:
        try:
            if self._classes is None:
                self._classes = self._load_graphiti_classes()
            driver = self._classes.falkor_driver(
                host=self._falkordb_host or "falkordb",
                port=self._falkordb_port or 6379,
                username=self._falkordb_username,
                password=self._falkordb_password,
                database="_graphiti_probe",
            )
            await driver.health_check()
            await driver.close()
            return True
        except Exception as exc:
            memory_logger.error("Graphiti connection test failed: %s", exc)
            return False

    def shutdown(self) -> None:
        self._clients.clear()
        self._initialized = False

    async def _query_edges(self, graphiti: Any, query: str, **params) -> list[dict]:
        records, _, _ = await graphiti.driver.execute_query(query, **params)
        return records or []

    async def _delete_source_facts(
        self, graphiti: Any, group_id: str, source_id: str
    ) -> None:
        # Episodes for one source_id are stored as Episodic nodes whose name
        # is either ``source_id`` (single-episode mode) or
        # ``"{source_id}#turn{idx}"`` (per-turn mode). Look up their UUIDs
        # and use Graphiti's remove_episode helper, which deletes the
        # episode's RELATES_TO edges plus any orphaned Entity nodes.
        episode_rows = await self._query_edges(
            graphiti,
            """
            MATCH (ep:Episodic)
            WHERE ep.group_id = $group_id
              AND (ep.name = $source_id OR ep.name STARTS WITH $prefix)
            RETURN ep.uuid AS uuid
            """,
            group_id=group_id,
            source_id=source_id,
            prefix=f"{source_id}#turn",
        )
        for row in episode_rows:
            try:
                await graphiti.remove_episode(row["uuid"])
            except Exception as exc:
                memory_logger.debug(
                    "remove_episode skipped for %s: %s", row["uuid"], exc
                )

    def _edge_to_memory_entry(self, edge: Any, user_id: str) -> MemoryEntry:
        return MemoryEntry(
            id=edge.uuid,
            content=edge.fact,
            metadata={
                "user_id": user_id,
                "provider": self.provider_identifier,
                "edge_uuid": edge.uuid,
                "name": edge.name,
                "episodes": list(edge.episodes or []),
                "valid_at": self._datetime_to_str(edge.valid_at),
                "invalid_at": self._datetime_to_str(edge.invalid_at),
                "expired_at": self._datetime_to_str(edge.expired_at),
                "reference_time": self._datetime_to_str(edge.reference_time),
            },
            created_at=self._datetime_to_str(edge.created_at),
        )

    def _row_to_memory_entry(self, row: dict, user_id: str) -> MemoryEntry:
        return MemoryEntry(
            id=row["uuid"],
            content=row.get("fact") or "",
            metadata={
                "user_id": user_id,
                "provider": self.provider_identifier,
                "edge_uuid": row["uuid"],
                "name": row.get("name"),
                "episodes": row.get("episodes") or [],
                "valid_at": self._datetime_to_str(row.get("valid_at")),
                "invalid_at": self._datetime_to_str(row.get("invalid_at")),
                "expired_at": self._datetime_to_str(row.get("expired_at")),
                "reference_time": self._datetime_to_str(row.get("reference_time")),
            },
            created_at=self._datetime_to_str(row.get("created_at")),
        )

    def _datetime_to_str(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _graphiti_group_id(self, user_id: str) -> str:
        # Graphiti's Falkor fulltext path is strict about token syntax; keep
        # group ids alnum+underscore only to avoid parser issues.
        return f"chronicle_{sanitize_user_id_for_graph(user_id).replace('-', '_')}"
