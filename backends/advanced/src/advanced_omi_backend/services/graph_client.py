"""Shared FalkorDB graph client utilities for the advanced OMI backend."""

import logging
import re
from typing import Any, Optional

from falkordb import Edge, FalkorDB, Node

logger = logging.getLogger(__name__)

# Per-user FalkorDB graphs are named ``chronicle_<sanitized_user_id>``. All
# Chronicle services (memory, knowledge graph, obsidian) share one graph per
# user, so cross-instance retrieval is naturally isolated and a user's data
# can be inspected/dropped by graph rather than by property filter.
CHRONICLE_GRAPH_PREFIX = "chronicle"

# FalkorDB graph names are unquoted Redis keys; keep the suffix to a safe
# ASCII subset. Real user_ids are MongoDB ObjectId hex (24 chars [0-9a-f])
# and benchmark user_ids look like ``bench-<question_id>`` — both already
# safe. Anything outside ``[a-zA-Z0-9_-]`` from arbitrary callers is mapped
# to ``_`` so we never produce a name FalkorDB will reject silently. ``-``
# is preserved because the bench user_ids embed it and it's idiomatic in
# Redis key namespacing.
_GRAPH_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_\-]")


def sanitize_user_id_for_graph(user_id: str) -> str:
    """Return a FalkorDB-safe suffix for ``user_id``.

    Empty/None user_id falls back to ``"anon"`` so we never construct a
    bare ``chronicle_`` name. Idempotent.
    """
    if not user_id:
        return "anon"
    return _GRAPH_NAME_UNSAFE.sub("_", user_id)


def graph_name_for_user(user_id: str) -> str:
    """Per-user graph name shared across chronicle memory, KG, and obsidian."""
    return f"{CHRONICLE_GRAPH_PREFIX}_{sanitize_user_id_for_graph(user_id)}"


def _convert_value(value: Any) -> Any:
    """Convert FalkorDB result values to plain Python types.

    Nodes/Edges become their .properties dict so downstream code that
    does ``dict(row["e"])`` keeps working.
    """
    if isinstance(value, Node):
        return value.properties
    if isinstance(value, Edge):
        props = dict(value.properties)
        props.setdefault("_type", value.relation)
        return props
    if isinstance(value, list):
        return [_convert_value(v) for v in value]
    return value


def _result_to_dicts(result) -> list[dict[str, Any]]:
    """Convert a FalkorDB query result to a list of dicts.

    FalkorDB returns ``result.header`` as ``[[type_int, col_name], ...]``
    and ``result.result_set`` as ``[[val, ...], ...]``.
    """
    if not result.result_set:
        return []

    headers = [h[1] for h in result.header]
    rows: list[dict[str, Any]] = []
    for record in result.result_set:
        row = {}
        for col_name, value in zip(headers, record):
            row[col_name] = _convert_value(value)
        rows.append(row)
    return rows


class _SessionProxy:
    """Drop-in replacement for a Neo4j-style session context manager.

    Allows code using ``with client.session() as s: s.run(query, **params)``
    to work unchanged.
    """

    def __init__(self, graph, read_only: bool = False):
        self._graph = graph
        self._read_only = read_only

    def run(self, cypher: str, **parameters) -> list[dict[str, Any]]:
        fn = self._graph.ro_query if self._read_only else self._graph.query
        result = fn(cypher, parameters or None)
        return _result_to_dicts(result)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class GraphClient:
    """Thin wrapper around the FalkorDB client for shared connection management."""

    def __init__(
        self,
        host: str = "falkordb",
        port: int = 6379,
        graph_name: str = "chronicle",
    ):
        self.host = host
        self.port = port
        self.graph_name = graph_name
        self._db: Optional[FalkorDB] = None
        self._graph = None

    def _ensure_connected(self):
        if self._db is None:
            self._db = FalkorDB(host=self.host, port=self.port)
            self._graph = self._db.select_graph(self.graph_name)

    @property
    def graph(self):
        self._ensure_connected()
        return self._graph

    def session(self, read_only: bool = False):
        """Return a session-like context manager for compatibility."""
        self._ensure_connected()
        return _SessionProxy(self._graph, read_only=read_only)

    def close(self):
        self._db = None
        self._graph = None

    def reset(self):
        self.close()

    def delete_graph(self) -> None:
        """Drop the underlying FalkorDB graph entirely (``GRAPH.DELETE``).

        Used by per-user wipe paths (``delete_all_user_memories``) so we drop
        the whole graph instead of issuing per-label DETACH DELETE sweeps.
        Safe to call against a non-existent graph — ``select_graph`` returns
        a handle and ``.delete()`` raises if it has never been written to;
        the caller wraps in try/except.
        """
        self._ensure_connected()
        self._graph.delete()
        self._db = None
        self._graph = None


class GraphInterface:
    """Access interface with a fixed access mode (read/write)."""

    def __init__(self, client: GraphClient, read_only: bool = False):
        self.client = client
        self.read_only = read_only

    def session(self):
        return self.client.session(read_only=self.read_only)

    def run(self, cypher: str, **parameters) -> list[dict[str, Any]]:
        """Run a query and return results as a list of dicts."""
        graph = self.client.graph
        fn = graph.ro_query if self.read_only else graph.query
        result = fn(cypher, parameters or None)
        return _result_to_dicts(result)


class GraphReadInterface(GraphInterface):
    def __init__(self, client: GraphClient):
        super().__init__(client, read_only=True)


class GraphWriteInterface(GraphInterface):
    def __init__(self, client: GraphClient):
        super().__init__(client, read_only=False)
