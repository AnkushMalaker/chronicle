"""
Database configuration and utilities for the Chronicle backend.

This module provides centralized database access to avoid duplication
across main.py and router modules.
"""

import logging
import os

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

# MongoDB Configuration
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "chronicle")

# 20s suits the request/response paths this client was tuned for, but it is wrong for a
# process that streams a whole collection: the archive export scans every audio chunk
# through one cursor and a single slow getMore kills the backup. PyMongo gives explicit
# kwargs precedence over URI options, so a connection string cannot override this --
# hence the env var, which admin CLIs raise for their own process only.
MONGODB_SOCKET_TIMEOUT_MS = int(os.getenv("MONGODB_SOCKET_TIMEOUT_MS", "20000"))

mongo_client = AsyncIOMotorClient(
    MONGODB_URI,
    w="majority",
    journal=True,
    maxPoolSize=50,  # Increased pool size for concurrent operations
    minPoolSize=10,  # Keep minimum connections ready
    maxIdleTimeMS=45000,  # Keep idle connections for 45 seconds
    serverSelectionTimeoutMS=5000,  # Fail fast if server unavailable
    socketTimeoutMS=MONGODB_SOCKET_TIMEOUT_MS,
)
db = mongo_client.get_default_database(MONGODB_DATABASE)

# Collection references (for non-Beanie collections)
users_col = db["users"]

# Note: conversations collection managed by Beanie (Document model)
# Note: processing_runs replaced by RQ job tracking
# Beanie initialization happens in main.py during application startup


def get_database():
    """Get the MongoDB database instance."""
    return db


def get_collections():
    """Get commonly used collection references."""
    return {
        "users_col": users_col,
    }
