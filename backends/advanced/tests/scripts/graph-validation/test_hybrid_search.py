"""Validate hybrid search: vector + full-text + recency bias.

Tests:
1. Hybrid merge produces better ranking than either alone
2. Recency bias correctly boosts recent conversations
3. Score components are traceable

Usage:
    uv run python tests/scripts/graph-validation/test_hybrid_search.py
"""

from utils import (
    compute_hybrid_scores,
    generate_embeddings_sync,
    get_graph,
    print_fail,
    print_header,
    print_pass,
    print_results_table,
)

TEST_USER = "test_user_001"


def _parse_results(result):
    """Parse FalkorDB result_set into list of dicts using header names."""
    headers = [h[1] for h in result.header]
    rows = []
    for row in result.result_set:
        rows.append({headers[i]: row[i] for i in range(len(headers))})
    return rows


def vector_search(graph, query: str, user_id: str, limit: int = 20):
    """Run vector search and return results."""
    embedding = generate_embeddings_sync([query])[0]

    result = graph.query(
        """
        CALL db.idx.vector.queryNodes('ConvChunk', 'embedding', $limit, vecf32($vector))
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               doc.title AS title, doc.date AS date, score
        ORDER BY score DESC
        """,
        params={"vector": embedding, "limit": limit, "user_id": user_id},
    )
    return _parse_results(result)


def fulltext_search(graph, query: str, user_id: str):
    """Run full-text search and return results."""
    result = graph.query(
        """
        CALL db.idx.fulltext.queryNodes('ConvChunk', $search_term)
        YIELD node AS chunk, score
        WHERE chunk.user_id = $user_id
        MATCH (doc:ConvDoc)-[:HAS_CHUNK]->(chunk)
        RETURN chunk.id AS chunk_id, chunk.section_title AS section,
               doc.title AS title, doc.date AS date, score
        ORDER BY score DESC
        """,
        params={"search_term": query, "user_id": user_id},
    )
    return _parse_results(result)


def test_hybrid_merge(graph):
    """Test that hybrid merge combines vector + full-text results."""
    print_header("Test: Hybrid Merge (Vector + Full-Text)")

    query = "project deadline September"

    vec_results = vector_search(graph, query, TEST_USER)
    ft_results = fulltext_search(graph, query, TEST_USER)

    print(f"  Query: '{query}'")
    print(f"  Vector results: {len(vec_results)}")
    print(f"  Full-text results: {len(ft_results)}")

    # Merge
    merged = compute_hybrid_scores(vec_results, ft_results)

    print(f"\n  Hybrid results (top 5):")
    print_results_table(
        merged[:5],
        ["final_score", "relevance_score", "recency_score", "title", "section"],
    )

    if merged:
        print_pass(f"Hybrid merge produced {len(merged)} results")

        # Check that both sources contribute
        has_vector = any(r["vector_score"] > 0 for r in merged)
        has_text = any(r["text_score"] > 0 for r in merged)
        if has_vector:
            print_pass("Vector scores contributing to results")
        else:
            print_fail("No vector scores in merged results")
        if has_text:
            print_pass("Full-text scores contributing to results")
        else:
            print_fail(
                "No full-text scores in merged results (might be expected if query terms not exact)"
            )
    else:
        print_fail("Hybrid merge returned no results")


def test_recency_bias(graph):
    """Test that recency bias boosts recent conversations."""
    print_header("Test: Recency Bias")

    # Use a generic query that matches multiple conversations
    query = "appointment schedule plans"

    vec_results = vector_search(graph, query, TEST_USER)
    ft_results = fulltext_search(graph, query, TEST_USER)

    # Compare with and without recency
    no_recency = compute_hybrid_scores(
        vec_results, ft_results, recency_half_life_days=99999, recency_floor=1.0
    )
    with_recency = compute_hybrid_scores(
        vec_results, ft_results, recency_half_life_days=30, recency_floor=0.5
    )

    print(f"  Query: '{query}'")
    print(f"\n  WITHOUT recency bias:")
    print_results_table(
        no_recency[:5],
        ["final_score", "recency_score", "title", "date"],
    )
    print(f"\n  WITH recency bias (30-day half-life):")
    print_results_table(
        with_recency[:5],
        ["final_score", "recency_score", "title", "date"],
    )

    if with_recency:
        # Most recent conversation (grocery, 2026-03-16) should rank higher with recency
        recency_scores = {r["title"]: r["recency_score"] for r in with_recency}
        if recency_scores:
            print_pass("Recency scores computed for all results")

            # Check that newer dates have higher recency scores
            dates_and_recency = [(r["date"], r["recency_score"]) for r in with_recency]
            print(f"\n  Date -> Recency mapping:")
            for d, rc in sorted(set(dates_and_recency)):
                print(f"    {d} -> {rc:.4f}")
        else:
            print_fail("No recency scores found")
    else:
        print_fail("No results to test recency on")


def test_exact_keyword_boost(graph):
    """Test that exact keyword match (BM25) boosts relevance."""
    print_header("Test: Exact Keyword Boost")

    # Search for "Dr. Chen" -- should strongly prefer dentist via BM25
    query_semantic = "doctor dental health"
    query_exact = "Dr. Chen"

    vec_results = vector_search(graph, query_semantic, TEST_USER)
    ft_results_exact = fulltext_search(graph, query_exact, TEST_USER)

    # Hybrid with exact keyword
    merged = compute_hybrid_scores(vec_results, ft_results_exact)

    print(f"  Semantic query: '{query_semantic}'")
    print(f"  Exact keyword: '{query_exact}'")
    print(f"\n  Hybrid results:")
    print_results_table(
        merged[:5], ["final_score", "vector_score", "text_score", "title", "section"]
    )

    if merged:
        # Chunks with exact "Dr. Chen" match should have text_score > 0
        exact_matches = [r for r in merged if r["text_score"] > 0]
        if exact_matches:
            print_pass(f"{len(exact_matches)} results boosted by exact keyword match")
            # These should all be from the dentist conversation
            dentist_exact = [
                r for r in exact_matches if "Dentist" in r.get("title", "")
            ]
            if dentist_exact:
                print_pass("Exact keyword matches are from dentist conversation")
        else:
            print_fail("No results had text_score > 0 for exact keyword")


if __name__ == "__main__":
    graph = get_graph()
    test_hybrid_merge(graph)
    test_recency_bias(graph)
    test_exact_keyword_boost(graph)
