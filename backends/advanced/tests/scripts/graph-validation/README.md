# FalkorDB Graph Memory Validation

Standalone scripts to validate using FalkorDB as the unified memory store (replacing Qdrant) before modifying Chronicle's production code.

## Prerequisites

- FalkorDB running (`docker compose up falkordb -d`)
- OpenAI API key in `backends/advanced/.env`
- Python deps: `falkordb`, `openai`, `python-dotenv` (all in existing pyproject.toml)

## Quick Start

```bash
cd backends/advanced

# 1. Start FalkorDB
docker compose up falkordb -d

# 2. Create schema (indexes + constraints)
uv run python tests/scripts/graph-validation/setup_schema.py

# 3. Insert sample data (generates real embeddings)
uv run python tests/scripts/graph-validation/sample_data.py

# 4. Run tests
uv run python tests/scripts/graph-validation/test_vector_search.py
uv run python tests/scripts/graph-validation/test_fulltext_search.py
uv run python tests/scripts/graph-validation/test_hybrid_search.py
uv run python tests/scripts/graph-validation/test_entity_graph.py
uv run python tests/scripts/graph-validation/test_conversation_doc.py

# 5. Cleanup
uv run python tests/scripts/graph-validation/setup_schema.py --cleanup
```

## Connection

FalkorDB defaults:
- **Host**: `localhost`
- **Port**: `6381`
- **Graph name**: `chronicle`

Override via environment variables: `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_GRAPH`.

## What Each Test Validates

| Script | Tests |
|--------|-------|
| `test_vector_search.py` | Semantic similarity, score ordering, user_id scoping |
| `test_fulltext_search.py` | BM25 keyword search, multi-term AND, domain terms, empty results |
| `test_hybrid_search.py` | Vector+BM25 merge, recency bias, exact keyword boost |
| `test_entity_graph.py` | Entity traversal, cross-entity queries, entity listing |
| `test_conversation_doc.py` | LLM doc generation, section parsing, entity extraction reliability |

## Test Labels (ConvDoc/ConvChunk/ConvEntity)

All test data uses `Conv*` prefixed labels to avoid conflicting with existing schema.
