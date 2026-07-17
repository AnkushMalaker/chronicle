"""Insert sample conversation data into FalkorDB for validation.

Reads sample .md files, chunks them by ### headers, generates real embeddings,
and stores as ConvDoc/ConvChunk/ConvEntity nodes with relationships.

Usage:
    uv run python tests/scripts/graph-validation/sample_data.py
"""

import uuid
from pathlib import Path

from utils import (
    generate_embeddings_sync,
    get_graph,
    parse_frontmatter,
    parse_people_section,
    print_fail,
    print_header,
    print_pass,
    split_on_headers,
)

SAMPLE_DIR = Path(__file__).parent / "sample_conversations"
TEST_USER_ID = "test_user_001"
OTHER_USER_ID = "other_user_002"  # For user-scoping tests


def insert_conversation(graph, md_path: Path, user_id: str):
    """Parse a conversation .md and insert into FalkorDB."""
    content = md_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(content)
    chunks = split_on_headers(content)
    people = parse_people_section(content)

    conv_id = frontmatter.get("conversation_id", md_path.stem)
    date = frontmatter.get("date", "2026-03-15T00:00:00")
    speakers = frontmatter.get("speakers", "")

    # Extract title from first ## header
    import re

    title_match = re.search(r"^##\s+(.+)$", content, re.MULTILINE)
    title = title_match.group(1) if title_match else md_path.stem

    print(f"\n  Processing: {title}")
    print(f"    Chunks: {len(chunks)}, People: {len(people)}")

    # Generate embeddings for all chunks
    chunk_texts = [f"{c['section_title']}: {c['text']}" for c in chunks]
    if not chunk_texts:
        print_fail(f"No chunks extracted from {md_path.name}")
        return

    print(f"    Generating embeddings for {len(chunk_texts)} chunks...")
    embeddings = generate_embeddings_sync(chunk_texts)
    print(f"    Embeddings generated ({len(embeddings[0])} dimensions)")

    # Create ConvDoc node
    graph.query(
        """
        MERGE (d:ConvDoc {conversation_id: $conv_id})
        SET d.title = $title,
            d.date = $date,
            d.user_id = $user_id,
            d.speakers = $speakers,
            d.file_path = $file_path
        """,
        params={
            "conv_id": conv_id,
            "title": title,
            "date": date,
            "user_id": user_id,
            "speakers": speakers,
            "file_path": str(md_path.relative_to(SAMPLE_DIR.parent)),
        },
    )

    # Create ConvChunk nodes with embeddings and link to ConvDoc
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        chunk_id = f"{conv_id}_chunk_{i:03d}"
        graph.query(
            """
            MATCH (d:ConvDoc {conversation_id: $conv_id})
            MERGE (c:ConvChunk {id: $chunk_id})
            SET c.text = $text,
                c.section_title = $section_title,
                c.embedding = vecf32($embedding),
                c.user_id = $user_id,
                c.created_at = $date,
                c.chunk_index = $chunk_index
            MERGE (d)-[:HAS_CHUNK]->(c)
            """,
            params={
                "conv_id": conv_id,
                "chunk_id": chunk_id,
                "text": chunk["text"],
                "section_title": chunk["section_title"],
                "embedding": embedding,
                "user_id": user_id,
                "date": date,
                "chunk_index": i,
            },
        )

    # Create ConvEntity nodes from People section and link to chunks
    for person in people:
        entity_id = f"{user_id}_{person['name'].lower().replace(' ', '_')}"
        graph.query(
            """
            MERGE (e:ConvEntity {id: $entity_id})
            SET e.name = $name,
                e.description = $description,
                e.type = 'person',
                e.user_id = $user_id
            """,
            params={
                "entity_id": entity_id,
                "name": person["name"],
                "description": person["description"],
                "user_id": user_id,
            },
        )

        # Link entity to all chunks that mention their name
        for i, chunk in enumerate(chunks):
            if person["name"].lower() in chunk["text"].lower():
                chunk_id = f"{conv_id}_chunk_{i:03d}"
                graph.query(
                    """
                    MATCH (c:ConvChunk {id: $chunk_id})
                    MATCH (e:ConvEntity {id: $entity_id})
                    MERGE (c)-[:MENTIONS]->(e)
                    """,
                    params={
                        "chunk_id": chunk_id,
                        "entity_id": entity_id,
                    },
                )

    print_pass(f"Inserted: {title} ({len(chunks)} chunks, {len(people)} entities)")


def main():
    print_header("Inserting Sample Data")

    graph = get_graph()

    # Insert conversations for the primary test user
    for md_file in sorted(SAMPLE_DIR.glob("*.md")):
        insert_conversation(graph, md_file, TEST_USER_ID)

    # Insert one conversation for a different user (for scoping tests)
    dentist_file = SAMPLE_DIR / "dentist_appointment.md"
    if dentist_file.exists():
        print(f"\n  Inserting duplicate for other user (scoping test)...")
        # We need to modify the conv_id to avoid UNIQUE constraint clash
        content = dentist_file.read_text(encoding="utf-8")
        # Write a temp copy with different conv_id
        temp_path = SAMPLE_DIR / "_other_user_dentist.md"
        temp_path.write_text(
            content.replace(
                "conversation_id: conv_002", "conversation_id: conv_002_other"
            )
        )
        insert_conversation(graph, temp_path, OTHER_USER_ID)
        temp_path.unlink()

    # Verify counts
    doc_result = graph.query("MATCH (d:ConvDoc) RETURN count(d) AS c")
    doc_count = doc_result.result_set[0][0]

    chunk_result = graph.query("MATCH (c:ConvChunk) RETURN count(c) AS c")
    chunk_count = chunk_result.result_set[0][0]

    entity_result = graph.query("MATCH (e:ConvEntity) RETURN count(e) AS c")
    entity_count = entity_result.result_set[0][0]

    rel_result = graph.query("MATCH ()-[r:HAS_CHUNK|MENTIONS]->() RETURN count(r) AS c")
    rel_count = rel_result.result_set[0][0]

    print_header("Summary")
    print_pass(f"ConvDoc nodes: {doc_count}")
    print_pass(f"ConvChunk nodes: {chunk_count}")
    print_pass(f"ConvEntity nodes: {entity_count}")
    print_pass(f"Relationships: {rel_count}")


if __name__ == "__main__":
    main()
