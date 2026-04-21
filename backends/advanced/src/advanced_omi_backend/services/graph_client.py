"""Shared FalkorDB graph client utilities for the advanced OMI backend."""

import logging
from typing import Any, Optional

from falkordb import Edge, FalkorDB, Node

logger = logging.getLogger(__name__)


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
