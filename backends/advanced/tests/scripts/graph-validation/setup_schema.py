"""Create FalkorDB schema for conversation memory validation.

Creates constraints, vector index, and full-text index using ConvDoc/ConvChunk
labels to avoid conflicting with existing schema.

Usage:
    uv run python tests/scripts/graph-validation/setup_schema.py
    uv run python tests/scripts/graph-validation/setup_schema.py --cleanup
"""

import sys

from utils import EMBEDDING_DIMENSIONS, get_graph, print_fail, print_header, print_pass


def setup(graph):
    print_header("Creating Schema")

    # Constraints — FalkorDB doesn't support IF NOT EXISTS, use try/except
    try:
        graph.query(
            "CREATE CONSTRAINT ON (d:ConvDoc) ASSERT d.conversation_id IS UNIQUE"
        )
        print_pass("Constraint: ConvDoc.conversation_id UNIQUE")
    except Exception as e:
        if "already exists" in str(e).lower() or "already indexed" in str(e).lower():
            print_pass("Constraint: ConvDoc.conversation_id UNIQUE (already exists)")
        else:
            print_fail(f"Constraint ConvDoc.conversation_id: {e}")

    try:
        graph.query("CREATE CONSTRAINT ON (c:ConvChunk) ASSERT c.id IS UNIQUE")
        print_pass("Constraint: ConvChunk.id UNIQUE")
    except Exception as e:
        if "already exists" in str(e).lower() or "already indexed" in str(e).lower():
            print_pass("Constraint: ConvChunk.id UNIQUE (already exists)")
        else:
            print_fail(f"Constraint ConvChunk.id: {e}")

    try:
        graph.query("CREATE CONSTRAINT ON (e:ConvEntity) ASSERT e.id IS UNIQUE")
        print_pass("Constraint: ConvEntity.id UNIQUE")
    except Exception as e:
        if "already exists" in str(e).lower() or "already indexed" in str(e).lower():
            print_pass("Constraint: ConvEntity.id UNIQUE (already exists)")
        else:
            print_fail(f"Constraint ConvEntity.id: {e}")

    # Vector index
    try:
        graph.query(
            f"CREATE VECTOR INDEX FOR (c:ConvChunk) ON (c.embedding) "
            f"OPTIONS {{dimension: {EMBEDDING_DIMENSIONS}, similarityFunction: 'cosine'}}"
        )
        print_pass(
            f"Vector index: ConvChunk.embedding ({EMBEDDING_DIMENSIONS}d, cosine)"
        )
    except Exception as e:
        if "already exists" in str(e).lower() or "already indexed" in str(e).lower():
            print_pass(
                f"Vector index: ConvChunk.embedding ({EMBEDDING_DIMENSIONS}d, cosine) (already exists)"
            )
        else:
            print_fail(f"Vector index: {e}")

    # Full-text index
    try:
        graph.query(
            "CREATE FULLTEXT INDEX FOR (c:ConvChunk) ON (c.text, c.section_title)"
        )
        print_pass("Full-text index: ConvChunk (text + section_title)")
    except Exception as e:
        if "already exists" in str(e).lower() or "already indexed" in str(e).lower():
            print_pass(
                "Full-text index: ConvChunk (text + section_title) (already exists)"
            )
        else:
            print_fail(f"Full-text index: {e}")

    # Verify indexes exist
    try:
        result = graph.query("CALL db.indexes()")
        idx_names = []
        for row in result.result_set:
            # Each row is a list; index name/label varies by FalkorDB version
            idx_names.append(str(row))
        print_pass(f"Indexes present: {len(result.result_set)} index(es) found")
    except Exception as e:
        print_fail(f"Could not verify indexes: {e}")

    print("\nSchema setup complete.")


def cleanup(graph):
    print_header("Cleaning Up Test Data")

    # Delete all test nodes and relationships
    result = graph.query(
        "MATCH (n) WHERE n:ConvDoc OR n:ConvChunk OR n:ConvEntity "
        "DETACH DELETE n RETURN count(n) AS deleted"
    )
    if result.result_set:
        deleted = result.result_set[0][0]
        print_pass(f"Deleted {deleted} test nodes")
    else:
        print_pass("No test nodes to delete")

    # Drop indexes — FalkorDB doesn't support IF EXISTS for DROP, use try/except
    for idx_query in [
        "DROP VECTOR INDEX ON :ConvChunk(embedding)",
        "DROP FULLTEXT INDEX ON :ConvChunk(text, section_title)",
    ]:
        try:
            graph.query(idx_query)
            print_pass(f"Dropped index: {idx_query}")
        except Exception as e:
            print_fail(f"Failed to drop index ({idx_query}): {e}")

    # Drop constraints
    for constraint_query in [
        "DROP CONSTRAINT ON (d:ConvDoc) ASSERT d.conversation_id IS UNIQUE",
        "DROP CONSTRAINT ON (c:ConvChunk) ASSERT c.id IS UNIQUE",
        "DROP CONSTRAINT ON (e:ConvEntity) ASSERT e.id IS UNIQUE",
    ]:
        try:
            graph.query(constraint_query)
            print_pass(f"Dropped constraint: {constraint_query}")
        except Exception as e:
            print_fail(f"Failed to drop constraint ({constraint_query}): {e}")

    print("\nCleanup complete.")


if __name__ == "__main__":
    graph = get_graph()
    if "--cleanup" in sys.argv:
        cleanup(graph)
    else:
        setup(graph)
