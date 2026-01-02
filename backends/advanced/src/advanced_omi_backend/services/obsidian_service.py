"""Obsidian vault ingestion service for Neo4j graph database.

This module provides functionality to parse, chunk, embed, and ingest Obsidian markdown
notes into a Neo4j graph database. It extracts notes, chunks them for vector search,
generates embeddings, and stores them with relationships (folders, tags, links) in Neo4j.

The service supports:
- Parsing Obsidian markdown files with frontmatter
- Character-based chunking with overlap
- Embedding generation using configured models
- Graph storage with Note, Chunk, Folder, Tag, and Link relationships
- Vector similarity search via Neo4j vector indexes
"""

import logging
import os
import re
import hashlib
from typing import TypedDict, List, Optional, Literal
from pathlib import Path
from advanced_omi_backend.services.memory.providers.llm_providers import (
    generate_openai_embeddings,
    chunk_text_with_spacy,
)
from advanced_omi_backend.services.memory.config import load_config_yml as load_root_config
from advanced_omi_backend.utils.model_utils import get_model_config
from advanced_omi_backend.utils.config_utils import resolve_value
from advanced_omi_backend.services.neo4j_client import (
    Neo4jClient,
    Neo4jReadInterface,
    Neo4jWriteInterface,
)

logger = logging.getLogger(__name__)


class NoteData(TypedDict):
    path: str
    name: str
    folder: str
    content: str
    wordcount: int
    links: List[str]
    tags: List[str]


class ChunkPayload(TypedDict):
    text: str
    embedding: List[float]


class ObsidianSearchResult(TypedDict):
    results: List[str]


class ObsidianSearchError(Exception):
    """Raised when Obsidian search fails at a specific stage."""

    def __init__(self, stage: Literal["embedding", "database"], message: str):
        super().__init__(message)
        self.stage = stage


def load_env_file(filepath: Path) -> dict[str, str]:
    """Load environment variables from a .env file.

    Args:
        filepath: Path to the .env file to load.

    Returns:
        Dictionary of key-value pairs from the .env file.
    """
    env_vars = {}
    if filepath.exists():
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    parts = line.split("=", 1)
                    key = parts[0].strip()
                    value = parts[1].strip() if len(parts) > 1 else ""
                    # Handle quotes
                    if (value.startswith("'") and value.endswith("'")) or (
                        value.startswith('"') and value.endswith('"')
                    ):
                        value = value[1:-1]
                    env_vars[key] = value
    return env_vars


class ObsidianService:
    """Service for ingesting Obsidian vaults into Neo4j graph database."""

    def __init__(self):
        """Initialize the Obsidian service with configuration from config.yml and environment."""
        # Resolve paths relative to this file
        # backends/advanced/src/advanced_omi_backend/services/obsidian_service.py
        self.CURRENT_DIR = Path(__file__).parent.resolve()

        # Load configuration strictly from standard locations
        # Prefer /app/config.yml inside containers (mounted by docker-compose)
        # Fallbacks handled by shared utility
        config_data = load_root_config()

        # Get model configurations using shared utility
        llm_config = get_model_config(config_data, "llm")
        if not llm_config:
            raise ValueError("Configuration for 'defaults.llm' not found in config.yml")

        embed_config = get_model_config(config_data, "embedding")
        if not embed_config:
            raise ValueError("Configuration for 'defaults.embedding' not found in config.yml")

        # Neo4j Connection - Prefer environment variables passed by Docker Compose
        neo4j_host = os.getenv("NEO4J_HOST")
        # Load .env file as fallback (for local dev or if env vars not set)
        candidate_env_files = [
            Path("/app/.env"),
            self.CURRENT_DIR.parent.parent.parent.parent
            / ".env",  # Project root .env file ToDo cleanup needed after k8s is migrated and there is no .env file in the project root.
            self.CURRENT_DIR.parent.parent.parent.parent
            / "backends"
            / "advanced"
            / ".env",  # repo path
        ]
        env_data = {}
        for p in candidate_env_files:
            if p.exists():
                env_data.update(load_env_file(p))

        # Use env var first, then fallback to .env file
        if not neo4j_host:
            neo4j_host = env_data.get("NEO4J_HOST")

        if not neo4j_host:
            raise KeyError("NEO4J_HOST not found in environment or .env")

        self.neo4j_uri = f"bolt://{neo4j_host}:7687"
        self.neo4j_user = os.getenv("NEO4J_USER") or env_data.get("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD") or env_data.get("NEO4J_PASSWORD", "")

        # Models / API - Loaded strictly from config.yml
        self.embedding_model = str(resolve_value(embed_config["model_name"]))
        self.embedding_dimensions = int(resolve_value(embed_config["embedding_dimensions"]))
        self.openai_base_url = str(resolve_value(llm_config["model_url"]))
        self.openai_api_key = str(resolve_value(llm_config["api_key"]))

        # Chunking - uses shared spaCy/text fallback utility
        self.chunk_word_limit = 120

        self.neo4j_client = Neo4jClient(self.neo4j_uri, self.neo4j_user, self.neo4j_password)
        self.read_interface = Neo4jReadInterface(self.neo4j_client)
        self.write_interface = Neo4jWriteInterface(self.neo4j_client)

    def close(self):
        """Close the Neo4j driver connection and clean up resources."""
        self.neo4j_client.close()

    def reset_driver(self):
        """Reset the driver connection, forcing it to be recreated with current credentials."""
        self.neo4j_client.reset()

    def setup_database(self) -> None:
        """Create database constraints and vector index for notes and chunks."""
        # Reset driver to ensure we use current credentials
        self.reset_driver()
        with self.write_interface.session() as session:
            session.run(
                "CREATE CONSTRAINT note_path IF NOT EXISTS FOR (n:Note) REQUIRE n.path IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE"
            )
            index_query = f"""
            CREATE VECTOR INDEX chunk_embeddings IF NOT EXISTS
            FOR (c:Chunk) ON (c.embedding)
            OPTIONS {{indexConfig: {{
             `vector.dimensions`: {self.embedding_dimensions},
             `vector.similarity_function`: 'cosine'
            }}}}
            """
            session.run(index_query)

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace for embedding inputs."""
        return re.sub(r"\s+", " ", text).strip()

    def parse_obsidian_note(self, root: str, filename: str, vault_path: str) -> NoteData:
        """Parse an Obsidian markdown file and extract metadata.

        Args:
            root: Directory containing the file.
            filename: Name of the markdown file.
            vault_path: Root path of the Obsidian vault.

        Returns:
            NoteData dictionary with parsed content, links, tags, and metadata.
        """
        full_path = os.path.join(root, filename)
        # Vault path might be relative or absolute, handle it
        try:
            rel_path = os.path.relpath(full_path, vault_path)
        except ValueError:
            rel_path = filename  # Fallback if paths on different drives (Windows)

        # Robust reading with encoding fallbacks
        raw_text = None
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                with open(full_path, "r", encoding=enc, errors="strict") as f:
                    raw_text = f.read()
                break
            except UnicodeDecodeError:
                continue
        if raw_text is None:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                raw_text = f.read()

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw_text, re.DOTALL)
        content = raw_text[fm_match.end() :] if fm_match else raw_text

        """
        Pattern breakdown:
        \[\[ matches [[
        ([^\]|]+) captures the link name (one or more chars except ] or |)
        (?:\|[^\]]+)? optionally matches |display text
        \]\] matches ]]
        Matches: [[note]] and [[note|display text]]
        """
        links = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
        tags = re.findall(r"#([a-zA-Z0-9_\-/]+)", content)

        return {
            "path": rel_path,
            "name": filename.replace(".md", ""),
            "folder": os.path.basename(root),
            "content": content.strip(),
            "wordcount": len(content.split()),
            "links": links,
            "tags": tags,
        }

    async def chunking_and_embedding(self, note_data: NoteData) -> List[ChunkPayload]:
        """Chunk note content and generate embeddings for each chunk.

        Args:
            note_data: Parsed note data to process.

        Returns:
            List of chunk payloads with text and embedding vectors.
        """
        text_chunks = chunk_text_with_spacy(
            note_data["content"],
            max_tokens=self.chunk_word_limit,
        )
        logger.info(
            f"Processing: {note_data['path']} ({len(note_data['content'])} chars -> {len(text_chunks)} chunks)"
        )

        cleaned_chunks: List[str] = []
        original_chunks: List[str] = []
        for txt in text_chunks:
            cleaned = self._clean_text(txt)
            if cleaned:
                cleaned_chunks.append(cleaned)
                original_chunks.append(txt)
            else:
                logger.warning(f"Skipping empty chunk for {note_data['path']}")

        if not cleaned_chunks:
            return []

        try:
            vectors = await generate_openai_embeddings(
                cleaned_chunks,
                api_key=self.openai_api_key,
                base_url=self.openai_base_url,
                model=self.embedding_model,
            )
        except Exception as e:
            logger.exception(f"Embedding generation failed for {note_data['path']}: {e}")
            return []

        chunk_payloads: List[ChunkPayload] = []
        for orig_text, vector in zip(original_chunks, vectors):
            if vector:
                chunk_payloads.append({"text": orig_text, "embedding": vector})
            else:
                logger.warning(f"Embedding missing for chunk in {note_data['path']}")

        return chunk_payloads

    def ingest_note_and_chunks(self, note_data: NoteData, chunks: List[ChunkPayload]) -> None:
        """Store note and chunks in Neo4j with relationships to folders, tags, and links.

        Args:
            note_data: Parsed note data to store.
            chunks: List of chunks with embeddings to store.
        """
        with self.write_interface.session() as session:
            session.run(
                """
                MERGE (f:Folder {name: $folder})
                MERGE (n:Note {path: $path})
                SET n.name = $name, n.wordcount = $wordcount
                MERGE (n)-[:IN_FOLDER]->(f)
            """,
                path=note_data["path"],
                name=note_data["name"],
                folder=note_data["folder"],
                wordcount=note_data["wordcount"],
            )

            for i, chunk in enumerate(chunks):
                chunk_id = hashlib.md5(f"{note_data['path']}_{i}".encode()).hexdigest()
                session.run(
                    """
                    MATCH (n:Note {path: $path})
                    MERGE (c:Chunk {id: $chunk_id})
                    SET c.text = $text, c.embedding = $embedding, c.index = $index
                    MERGE (n)-[:HAS_CHUNK]->(c)
                """,
                    path=note_data["path"],
                    chunk_id=chunk_id,
                    text=chunk["text"],
                    embedding=chunk["embedding"],
                    index=i,
                )

            for tag in note_data["tags"]:
                session.run(
                    "MATCH (n:Note {path: $path}) MERGE (t:Tag {name: $tag}) MERGE (n)-[:HAS_TAG]->(t)",
                    path=note_data["path"],
                    tag=tag,
                )
            for link in note_data["links"]:
                session.run(
                    "MATCH (source:Note {path: $path}) MERGE (target:Note {name: $link}) "
                    "ON CREATE SET target.path = $link + '.md' MERGE (source)-[:LINKS_TO]->(target)",
                    path=note_data["path"],
                    link=link,
                )

    async def ingest_vault(self, vault_path: str) -> dict:
        """Ingest an entire Obsidian vault into Neo4j.

        Processes all markdown files in the vault, chunks them, generates embeddings,
        and stores them in Neo4j with relationships.

        Args:
            vault_path: Path to the Obsidian vault directory.

        Returns:
            Dictionary with status, processed count, and any errors.

        Raises:
            FileNotFoundError: If vault path doesn't exist.
        """
        if not os.path.exists(vault_path):
            raise FileNotFoundError(f"Vault path not found: {vault_path}")

        self.setup_database()

        processed = 0
        errors = []

        for root, dirs, files in os.walk(vault_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                if file.endswith(".md"):
                    try:
                        note_data = self.parse_obsidian_note(root, file, vault_path)
                        chunk_payloads = await self.chunking_and_embedding(note_data)

                        if chunk_payloads:
                            self.ingest_note_and_chunks(note_data, chunk_payloads)
                            processed += 1
                    except Exception as e:
                        logger.exception(f"Processing {file} failed")
                        errors.append(f"{file}: {str(e)}")

        return {"status": "success", "processed": processed, "errors": errors}

    async def search_obsidian(self, query: str, limit: int = 5) -> ObsidianSearchResult:
        """Search Obsidian vault for relevant context using vector search and graph traversal.

        Args:
            query: User's search query.
            limit: Maximum number of chunks to retrieve.

        Returns:
            Result payload containing the formatted contexts (if any) and structured error info.
        """
        try:
            clean_query = self._clean_text(query)
            vectors = await generate_openai_embeddings(
                [clean_query],
                api_key=self.openai_api_key,
                base_url=self.openai_base_url,
                model=self.embedding_model,
            )
            query_vector = vectors[0] if vectors else None
        except Exception as exc:
            logger.error("Obsidian search embedding failed: %s", exc)
            raise ObsidianSearchError("embedding", str(exc)) from exc

        if not query_vector:
            return {"results": []}

        cypher_query = """
        CALL db.index.vector.queryNodes('chunk_embeddings', $limit, $vector)
        YIELD node AS chunk, score
        
        // Find the parent Note
        MATCH (note:Note)-[:HAS_CHUNK]->(chunk)
        
        // Get graph context: What tags and linked files are around this note?
        OPTIONAL MATCH (note)-[:HAS_TAG]->(t:Tag)
        OPTIONAL MATCH (note)-[:LINKS_TO]->(linked:Note)
        
        RETURN 
            note.name AS source,
            chunk.text AS content,
            collect(DISTINCT t.name) AS tags,
            collect(DISTINCT linked.name) AS outgoing_links,
            score
        ORDER BY score DESC
        """

        context_entries = []
        try:
            with self.read_interface.session() as session:
                results = session.run(cypher_query, vector=query_vector, limit=limit)

                for record in results:
                    entry = f"SOURCE: {record['source']}\n"
                    tags = record["tags"]
                    if tags:
                        entry += f"TAGS: {', '.join(tags)}\n"
                    links = record["outgoing_links"]
                    if links:
                        entry += f"RELATED NOTES: {', '.join(links)}\n"
                    entry += f"CONTENT: {record['content']}\n"
                    entry += "---"
                    context_entries.append(entry)
        except Exception as e:
            logger.error(f"Obsidian search failed: {e}")
            raise ObsidianSearchError("database", str(e)) from e

        return {"results": context_entries}


# Lazy initialization to avoid startup failures
_obsidian_service: Optional[ObsidianService] = None


def get_obsidian_service() -> ObsidianService:
    """Get or create the Obsidian service singleton.

    Returns:
        ObsidianService instance, creating it on first access.
    """
    global _obsidian_service
    if _obsidian_service is None:
        _obsidian_service = ObsidianService()
    return _obsidian_service


# Backward compatibility: module-level access uses lazy initialization
# This property-like access ensures the service is only created when first used
class _ObsidianServiceProxy:
    """Proxy for lazy access to obsidian_service."""

    def __getattr__(self, name):
        return getattr(get_obsidian_service(), name)


obsidian_service = _ObsidianServiceProxy()
