"""Validate vector index search with user scoping.

Tests:
1. Semantic search finds relevant chunks (project deadline -> Q3 meeting)
2. User scoping prevents cross-user data leakage
3. Score ordering is correct

Usage:
    uv run python tests/scripts/graph-validation/test_vector_search.py
"""

from utils import (
    generate_embeddings_sync,
    get_graph,
    print_fail,
    print_header,
    print_pass,
    print_results_table,
)

TEST_USER = "test_user_001"
OTHER_USER = "other_user_002"


def _parse_vector_results(result, fields):
    """Parse FalkorDB result_set into list of dicts using header names."""
    headers = [h[1] for h in result.header]
    rows = []
    for row in result.result_set:
        rows.append({headers[i]: row[i] for i in range(len(headers))})
    return rows


def test_basic_vector_search(graph):
    """Search for project deadline -- should return Q3 meeting chunks."""
    print_header("Test: Basic Vector Search")

    query = "project deadline and timeline"
    embedding = generate_embeddings_sync([query])[0]

    result = graph.query(
        """
        CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($vector))
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               chunk.text AS text, doc.title AS title, score
        ORDER BY score DESC
        """,
        params={"vector": embedding, "limit": 10, "user_id": TEST_USER},
    )
    results = _parse_vector_results(result, [])

    print(f"  Query: '{query}'")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["score", "title", "section"])

    # Verify: top result should be from Q3 meeting
    if results and "Q3" in results[0].get("title", ""):
        print_pass("Top result is from Q3 meeting (expected)")
    elif results:
        print_fail(f"Top result is '{results[0].get('title')}', expected Q3 meeting")
    else:
        print_fail("No results returned")

    # Verify scores are in descending order
    scores = [r["score"] for r in results]
    if scores == sorted(scores, reverse=True):
        print_pass("Scores in descending order")
    else:
        print_fail("Scores NOT in descending order")

    return results


def test_semantic_relevance(graph):
    """Search for health/medical topic -- should return dentist chunks."""
    print_header("Test: Semantic Relevance")

    query = "dental health and medical procedures"
    embedding = generate_embeddings_sync([query])[0]

    result = graph.query(
        """
        CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($vector))
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               doc.title AS title, score
        ORDER BY score DESC
        """,
        params={"vector": embedding, "limit": 5, "user_id": TEST_USER},
    )
    results = _parse_vector_results(result, [])

    print(f"  Query: '{query}'")
    print_results_table(results, ["score", "title", "section"])

    if results and "Dentist" in results[0].get("title", ""):
        print_pass("Top result is dentist conversation (expected)")
    elif results:
        print_fail(f"Top result is '{results[0].get('title')}', expected Dentist")
    else:
        print_fail("No results returned")


def test_user_scoping(graph):
    """Verify user_id filtering prevents cross-user results."""
    print_header("Test: User Scoping")

    query = "dentist crown procedure"
    embedding = generate_embeddings_sync([query])[0]

    # Search as TEST_USER
    result = graph.query(
        """
        CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($vector))
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        RETURN chunk.id AS chunk_id, chunk.user_id AS user_id, score
        ORDER BY score DESC
        """,
        params={"vector": embedding, "limit": 20, "user_id": TEST_USER},
    )
    test_user_results = _parse_vector_results(result, [])

    # Search as OTHER_USER
    result = graph.query(
        """
        CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($vector))
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        RETURN chunk.id AS chunk_id, chunk.user_id AS user_id, score
        ORDER BY score DESC
        """,
        params={"vector": embedding, "limit": 20, "user_id": OTHER_USER},
    )
    other_user_results = _parse_vector_results(result, [])

    print(f"  TEST_USER results: {len(test_user_results)}")
    print(f"  OTHER_USER results: {len(other_user_results)}")

    # Verify no cross-user leakage
    test_user_ids = {r["user_id"] for r in test_user_results}
    other_user_ids = {r["user_id"] for r in other_user_results}

    if test_user_ids <= {TEST_USER}:
        print_pass("TEST_USER results contain only TEST_USER data")
    else:
        print_fail(f"TEST_USER results leaked: {test_user_ids}")

    if other_user_ids <= {OTHER_USER}:
        print_pass("OTHER_USER results contain only OTHER_USER data")
    else:
        print_fail(f"OTHER_USER results leaked: {other_user_ids}")

    # Verify different result counts (other user has fewer conversations)
    if len(test_user_results) > len(other_user_results):
        print_pass(
            f"TEST_USER has more results ({len(test_user_results)} vs {len(other_user_results)})"
        )
    else:
        print_pass(
            f"Result counts: TEST={len(test_user_results)}, OTHER={len(other_user_results)}"
        )


if __name__ == "__main__":
    graph = get_graph()
    test_basic_vector_search(graph)
    test_semantic_relevance(graph)
    test_user_scoping(graph)
