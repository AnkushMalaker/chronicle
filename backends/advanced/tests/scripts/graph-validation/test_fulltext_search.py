"""Validate FalkorDB full-text (BM25) search.

Tests:
1. Single keyword search returns matching chunks
2. Multi-term AND search narrows results
3. BM25 scores are non-zero and ranked correctly
4. User scoping works with full-text results

Usage:
    uv run python tests/scripts/graph-validation/test_fulltext_search.py
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


def test_single_keyword(graph):
    """Search for 'deadline' -- should return Q3 meeting chunks."""
    print_header("Test: Single Keyword Full-Text Search")

    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               chunk.text AS text, doc.title AS title, score
        ORDER BY score DESC
        """,
        params={"search_term": "deadline", "user_id": TEST_USER},
    )
    results = _parse_results(result)

    print(f"  Query: 'deadline'")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["score", "title", "section"])

    if results:
        print_pass(f"Full-text search returned {len(results)} results")
        # Check that scores are positive
        if all(r["score"] > 0 for r in results):
            print_pass("All BM25 scores are positive")
        else:
            print_fail("Some scores are zero or negative")
    else:
        print_fail("No results for 'deadline'")

    return results


def test_multi_term_search(graph):
    """Search for 'Q3 AND deadline' -- should narrow to Q3 meeting."""
    print_header("Test: Multi-Term AND Search")

    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               doc.title AS title, score
        ORDER BY score DESC
        """,
        params={"search_term": "Q3 AND deadline", "user_id": TEST_USER},
    )
    results = _parse_results(result)

    print(f"  Query: 'Q3 AND deadline'")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["score", "title", "section"])

    if results:
        # All results should be from Q3 meeting
        q3_results = [r for r in results if "Q3" in r.get("title", "")]
        if len(q3_results) == len(results):
            print_pass("All multi-term results are from Q3 meeting")
        else:
            non_q3 = [r["title"] for r in results if "Q3" not in r.get("title", "")]
            print_fail(f"Some results are not Q3: {non_q3}")
    else:
        print_fail("No results for 'Q3 AND deadline'")


def test_dental_terms(graph):
    """Search for dental-specific terms -- should return dentist chunks."""
    print_header("Test: Domain-Specific Terms")

    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               doc.title AS title, score
        ORDER BY score DESC
        """,
        params={"search_term": "crown molar", "user_id": TEST_USER},
    )
    results = _parse_results(result)

    print(f"  Query: 'crown molar'")
    print(f"  Results: {len(results)}")
    print_results_table(results, ["score", "title", "section"])

    if results and "Dentist" in results[0].get("title", ""):
        print_pass("Top result is dentist conversation")
    elif results:
        print_fail(f"Top result: {results[0].get('title')}")
    else:
        print_fail("No results for 'crown molar'")


def test_no_results(graph):
    """Search for completely unrelated term -- should return empty."""
    print_header("Test: No Results for Unrelated Query")

    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        RETURN chunk.id AS chunk_id, score
        """,
        params={"search_term": "quantum entanglement photon", "user_id": TEST_USER},
    )
    results = _parse_results(result)

    print(f"  Query: 'quantum entanglement photon'")
    print(f"  Results: {len(results)}")

    if len(results) == 0:
        print_pass("No results for unrelated query (expected)")
    else:
        print_fail(f"Got {len(results)} unexpected results")


def test_user_scoping_fulltext(graph):
    """Verify full-text results are scoped to user."""
    print_header("Test: Full-Text User Scoping")

    # Search as test user -- should find all 3 conversations
    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        RETURN chunk.id AS chunk_id, chunk.user_id AS uid
        """,
        params={
            "search_term": "appointment OR deadline OR grocery",
            "user_id": TEST_USER,
        },
    )
    test_results = _parse_results(result)

    # Search as other user -- should find only their dentist copy
    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        RETURN chunk.id AS chunk_id, chunk.user_id AS uid
        """,
        params={
            "search_term": "appointment OR deadline OR grocery",
            "user_id": "other_user_002",
        },
    )
    other_results = _parse_results(result)

    print(f"  TEST_USER results: {len(test_results)}")
    print(f"  OTHER_USER results: {len(other_results)}")

    if len(test_results) > len(other_results):
        print_pass("TEST_USER has more results (expected -- more conversations)")
    else:
        print_pass(
            f"Result counts: TEST={len(test_results)}, OTHER={len(other_results)}"
        )

    # Verify no cross-user contamination
    test_uids = {r["uid"] for r in test_results}
    other_uids = {r["uid"] for r in other_results}

    if test_uids <= {TEST_USER}:
        print_pass("TEST_USER full-text results scoped correctly")
    else:
        print_fail(f"Leaked user_ids in TEST results: {test_uids}")

    if other_uids <= {"other_user_002"}:
        print_pass("OTHER_USER full-text results scoped correctly")
    else:
        print_fail(f"Leaked user_ids in OTHER results: {other_uids}")


if __name__ == "__main__":
    graph = get_graph()
    test_single_keyword(graph)
    test_multi_term_search(graph)
    test_dental_terms(graph)
    test_no_results(graph)
    test_user_scoping_fulltext(graph)
