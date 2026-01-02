"""Shared Neo4j client utilities for the advanced OMI backend."""

from typing import Optional
from neo4j import GraphDatabase, Driver, READ_ACCESS, WRITE_ACCESS


class Neo4jClient:
    """Thin wrapper around the Neo4j driver for shared connection management."""

    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.auth = (user, password)
        self._driver: Optional[Driver] = None

    def get_driver(self) -> Driver:
        if not self._driver:
            self._driver = GraphDatabase.driver(self.uri, auth=self.auth)
        return self._driver

    def session(self, access_mode: Optional[str] = None):
        driver = self.get_driver()
        if access_mode:
            return driver.session(default_access_mode=access_mode)
        return driver.session()

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def reset(self):
        self.close()


class Neo4jInterface:
    """Access interface with a fixed access mode (read/write)."""

    def __init__(self, client: Neo4jClient, access_mode: str):
        self.client = client
        self.access_mode = access_mode

    def session(self):
        return self.client.session(access_mode=self.access_mode)

    def run(self, query: str, **parameters):
        """Run a query and return consumed result data.

        Note: For queries requiring iteration over results, use session()
        directly with a context manager.
        """
        with self.session() as session:
            result = session.run(query, **parameters)
            return result.data()  # Consume result before session closes


class Neo4jReadInterface(Neo4jInterface):
    def __init__(self, client: Neo4jClient):
        super().__init__(client, access_mode=READ_ACCESS)


class Neo4jWriteInterface(Neo4jInterface):
    def __init__(self, client: Neo4jClient):
        super().__init__(client, access_mode=WRITE_ACCESS)
