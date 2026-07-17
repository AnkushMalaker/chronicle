"""Validate entity-based graph traversal search.

Tests:
1. Find chunks mentioning a specific person via graph traversal
2. Cross-entity query (conversations mentioning both X and Y)
3. Entity listing per user

Usage:
    uv run python tests/scripts/graph-validation/test_entity_graph.py
"""

from utils import get_graph, print_fail, print_header, print_pass, print_results_table

TEST_USER = "test_user_001"


def _parse_results(result):
    """Parse FalkorDB result_set into list of dicts using header names."""
    headers = [h[1] for h in result.header]
    rows = []
    for row in result.result_set:
        rows.append({headers[i]: row[i] for i in range(len(headers))})
    return rows


def test_entity_search(graph):
    """Search for chunks mentioning 'John' via graph traversal."""
    print_header("Test: Entity Graph Traversal -- 'John'")

    result = graph.query(
        """
        MATCH (e:ConvEntity {user_id: $user_id})
        WHERE toLower(e.name) CONTAINS toLower($entity_name)
        WITH e
        MATCH (chunk:ConvChunk)-[:MENTIONS]->(e)
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN e.name AS entity, chunk.id AS chunk_id,
               chunk.section_title AS section, chunk.text AS text,
               doc.title AS title, doc.date AS date
        ORDER BY doc.date DESC
        """,
        params={"user_id": TEST_USER, "entity_name": "John"},
    )
    results = _parse_results(result)

    print(f"  Entity: 'John'")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["entity", "title", "section"])

    if results:
        print_pass(f"Found {len(results)} chunks mentioning John")
        # All should be from Q3 meeting
        titles = {r["title"] for r in results}
        if all("Q3" in t for t in titles):
            print_pass("All John mentions are in Q3 meeting (expected)")
        else:
            print_fail(f"Unexpected titles: {titles}")
    else:
        print_fail("No results for entity 'John'")


def test_entity_search_dr_chen(graph):
    """Search for chunks mentioning 'Dr. Chen'."""
    print_header("Test: Entity Graph Traversal -- 'Dr. Chen'")

    result = graph.query(
        """
        MATCH (e:ConvEntity {user_id: $user_id})
        WHERE toLower(e.name) CONTAINS toLower($entity_name)
        WITH e
        MATCH (chunk:ConvChunk)-[:MENTIONS]->(e)
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN e.name AS entity, chunk.section_title AS section,
               doc.title AS title, doc.date AS date
        ORDER BY doc.date DESC
        """,
        params={"user_id": TEST_USER, "entity_name": "Dr. Chen"},
    )
    results = _parse_results(result)

    print(f"  Entity: 'Dr. Chen'")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["entity", "title", "section"])

    if results:
        print_pass(f"Found {len(results)} chunks mentioning Dr. Chen")
        if all("Dentist" in r.get("title", "") for r in results):
            print_pass("All Dr. Chen mentions are in dentist conversation (expected)")
    else:
        print_fail("No results for entity 'Dr. Chen'")


def test_cross_entity_query(graph):
    """Find conversations that mention BOTH John AND Sarah."""
    print_header("Test: Cross-Entity Query (John AND Sarah)")

    result = graph.query(
        """
        MATCH (e1:ConvEntity {user_id: $user_id})
        WHERE toLower(e1.name) CONTAINS 'john'
        MATCH (e2:ConvEntity {user_id: $user_id})
        WHERE toLower(e2.name) CONTAINS 'sarah'
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(c1:ConvChunk)-[:MENTIONS]->(e1)
        MATCH (doc)-[:HAS_CHUNK]->(c2:ConvChunk)-[:MENTIONS]->(e2)
        RETURN DISTINCT doc.conversation_id AS conv_id, doc.title AS title,
               collect(DISTINCT e1.name) AS entity1,
               collect(DISTINCT e2.name) AS entity2
        """,
        params={"user_id": TEST_USER},
    )
    results = _parse_results(result)

    print(f"  Cross-entity: John AND Sarah")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["title", "entity1", "entity2"])

    if results:
        print_pass(f"Found {len(results)} conversations mentioning both John and Sarah")
    else:
        print_fail("No conversations found with both John and Sarah")


def test_list_entities(graph):
    """List all entities for a user with mention counts."""
    print_header("Test: List All Entities")

    result = graph.query(
        """
        MATCH (e:ConvEntity {user_id: $user_id})
        OPTIONAL MATCH (chunk:ConvChunk)-[:MENTIONS]->(e)
        RETURN e.name AS name, e.type AS type, e.description AS description,
               count(chunk) AS mention_count
        ORDER BY mention_count DESC
        """,
        params={"user_id": TEST_USER},
    )
    results = _parse_results(result)

    print(f"  User: {TEST_USER}")
    print(f"  Entities: {len(results)}")
    print_results_table(results, ["name", "type", "description", "mention_count"])

    if results:
        print_pass(f"Found {len(results)} entities for user")
        # Should have at least John, Sarah, Dr. Chen, etc.
        names = {r["name"] for r in results}
        for expected in ["John", "Sarah", "Dr. Chen"]:
            if expected in names:
                print_pass(f"Entity '{expected}' found")
            else:
                print_fail(f"Entity '{expected}' not found in {names}")
    else:
        print_fail("No entities found")


if __name__ == "__main__":
    graph = get_graph()
    test_entity_search(graph)
    test_entity_search_dr_chen(graph)
    test_cross_entity_query(graph)
    test_list_entities(graph)
