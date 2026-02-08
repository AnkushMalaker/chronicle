"""
Event storage module for test plugin using SQLite.

Provides async SQLite operations for logging and querying plugin events.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)


class EventStorage:
    """SQLite-based event storage for test plugin"""

    def __init__(self, db_path: str = "/app/debug/test_plugin_events.db"):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Initialize database and create tables"""
        # Ensure directory exists
        logger.info(f"🔍 DEBUG: Initializing event storage with db_path={self.db_path}")

        db_dir = Path(self.db_path).parent
        logger.info(f"🔍 DEBUG: Database directory: {db_dir}")
        logger.info(f"🔍 DEBUG: Directory exists before mkdir: {db_dir.exists()}")

        try:
            db_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"🔍 DEBUG: Directory created/verified: {db_dir}")
            logger.info(f"🔍 DEBUG: Directory permissions: {oct(db_dir.stat().st_mode)}")
        except Exception as e:
            logger.error(f"🔍 DEBUG: Failed to create directory: {e}")
            raise

        logger.info(f"🔍 DEBUG: Attempting to connect to SQLite database...")
        try:
            self.db = await aiosqlite.connect(self.db_path)
            logger.info(f"🔍 DEBUG: Successfully connected to database")

            # Enable WAL mode for better concurrent access (allows concurrent reads/writes)
            # This fixes the "readonly database" error when Robot tests access from host
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s for locks
            logger.info(f"✓ Enabled WAL mode for concurrent access")

            # Set file permissions to 666 so host user can write (container runs as root)
            # Robot tests run as host user and need write access to the database
            try:
                os.chmod(self.db_path, 0o666)
                # Also set permissions on WAL and SHM files if they exist
                wal_file = f"{self.db_path}-wal"
                shm_file = f"{self.db_path}-shm"
                if os.path.exists(wal_file):
                    os.chmod(wal_file, 0o666)
                if os.path.exists(shm_file):
                    os.chmod(shm_file, 0o666)
                logger.info(f"✓ Set database file permissions to 666 for host access")
            except Exception as perm_error:
                logger.warning(f"Could not set database permissions: {perm_error}")

        except Exception as e:
            logger.error(f"🔍 DEBUG: Failed to connect to database: {e}")
            logger.error(f"🔍 DEBUG: Database file exists: {Path(self.db_path).exists()}")
            if Path(self.db_path).exists():
                logger.error(f"🔍 DEBUG: Database file permissions: {oct(Path(self.db_path).stat().st_mode)}")
            raise

        # Create events table
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS plugin_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                event TEXT NOT NULL,
                user_id TEXT NOT NULL,
                data TEXT NOT NULL,
                metadata TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create index for faster queries
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_type
            ON plugin_events(event)
        """)

        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_id
            ON plugin_events(user_id)
        """)

        await self.db.commit()
        logger.info(f"Event storage initialized at {self.db_path}")

    async def log_event(
        self,
        event: str,
        user_id: str,
        data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Log an event to the database.

        Args:
            event: Event name (e.g., 'transcript.batch')
            user_id: User ID from context
            data: Event data dictionary
            metadata: Optional metadata dictionary

        Returns:
            Row ID of inserted event
        """
        # Add at start
        logger.debug(f"💾 STORAGE: Logging event '{event}' for user {user_id}")

        if not self.db:
            logger.error("💾 STORAGE: Database connection not initialized!")
            raise RuntimeError("Event storage not initialized")

        timestamp = datetime.utcnow().isoformat()

        # Add before serialization
        logger.debug(f"💾 STORAGE: Serializing event data...")
        try:
            data_json = json.dumps(data)
            metadata_json = json.dumps(metadata) if metadata else None
        except Exception as e:
            logger.error(
                f"💾 STORAGE: JSON serialization failed for event '{event}': {e}",
                exc_info=True
            )
            raise

        # Add before database operation
        logger.debug(f"💾 STORAGE: Inserting into plugin_events table...")

        try:
            cursor = await self.db.execute(
                """
                INSERT INTO plugin_events (timestamp, event, user_id, data, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (timestamp, event, user_id, data_json, metadata_json)
            )

            await self.db.commit()
            row_id = cursor.lastrowid

            # Add success log
            logger.info(
                f"💾 STORAGE: Event '{event}' inserted successfully (row_id={row_id})"
            )

            return row_id

        except Exception as e:
            logger.error(
                f"💾 STORAGE: Database operation failed for event '{event}': {e}",
                exc_info=True
            )
            raise

    async def get_events_by_type(self, event: str) -> List[Dict[str, Any]]:
        """
        Query events by event type.

        Args:
            event: Event name to filter by

        Returns:
            List of event dictionaries
        """
        if not self.db:
            raise RuntimeError("Event storage not initialized")

        cursor = await self.db.execute(
            """
            SELECT id, timestamp, event, user_id, data, metadata, created_at
            FROM plugin_events
            WHERE event = ?
            ORDER BY created_at DESC
            """,
            (event,)
        )

        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def get_events_by_user(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Query events by user ID.

        Args:
            user_id: User ID to filter by

        Returns:
            List of event dictionaries
        """
        if not self.db:
            raise RuntimeError("Event storage not initialized")

        cursor = await self.db.execute(
            """
            SELECT id, timestamp, event, user_id, data, metadata, created_at
            FROM plugin_events
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,)
        )

        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def get_all_events(self) -> List[Dict[str, Any]]:
        """
        Get all logged events.

        Returns:
            List of all event dictionaries
        """
        if not self.db:
            raise RuntimeError("Event storage not initialized")

        cursor = await self.db.execute(
            """
            SELECT id, timestamp, event, user_id, data, metadata, created_at
            FROM plugin_events
            ORDER BY created_at DESC
            """
        )

        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def clear_events(self) -> int:
        """
        Clear all events from the database.

        Returns:
            Number of rows deleted
        """
        if not self.db:
            raise RuntimeError("Event storage not initialized")

        cursor = await self.db.execute("DELETE FROM plugin_events")
        await self.db.commit()

        deleted = cursor.rowcount
        logger.info(f"Cleared {deleted} events from database")

        return deleted

    async def get_event_count(self, event: Optional[str] = None) -> int:
        """
        Get count of events.

        Args:
            event: Optional event type to filter by

        Returns:
            Count of matching events
        """
        if not self.db:
            raise RuntimeError("Event storage not initialized")

        if event:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM plugin_events WHERE event = ?",
                (event,)
            )
        else:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM plugin_events"
            )

        row = await cursor.fetchone()
        return row[0] if row else 0

    def _rows_to_dicts(self, rows: List[tuple]) -> List[Dict[str, Any]]:
        """
        Convert database rows to dictionaries.

        Args:
            rows: List of database row tuples

        Returns:
            List of event dictionaries
        """
        events = []

        for row in rows:
            event_dict = {
                'id': row[0],
                'timestamp': row[1],
                'event': row[2],
                'user_id': row[3],
                'data': json.loads(row[4]) if row[4] else {},
                'metadata': json.loads(row[5]) if row[5] else {},
                'created_at': row[6]
            }

            # Flatten data fields to top level for easier access in tests
            if isinstance(event_dict['data'], dict):
                event_dict.update(event_dict['data'])

            events.append(event_dict)

        return events

    async def cleanup(self):
        """Close database connection"""
        if self.db:
            await self.db.close()
            logger.info("Event storage connection closed")
