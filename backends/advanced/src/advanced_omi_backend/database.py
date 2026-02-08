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

mongo_client = AsyncIOMotorClient(
    MONGODB_URI,
    maxPoolSize=50,  # Increased pool size for concurrent operations
    minPoolSize=10,  # Keep minimum connections ready
    maxIdleTimeMS=45000,  # Keep idle connections for 45 seconds
    serverSelectionTimeoutMS=5000,  # Fail fast if server unavailable
    socketTimeoutMS=20000,  # 20 second timeout for operations
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


