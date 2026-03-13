"""Database initialization and management for speaker recognition system."""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Create data directory if it doesn't exist
# Use the mounted volume path in Docker, fallback to local path for development
DATA_DIR = (
    Path("/app/data")
    if Path("/app/data").exists()
    else Path(__file__).parent.parent / "data"
)
DATA_DIR.mkdir(exist_ok=True)

# Database configuration
DATABASE_URL = f"sqlite:///{DATA_DIR}/speakers.db"

# Create engine with SQLite-specific settings
engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False
    },  # Allow SQLite to be used with multiple threads
    echo=False,  # Set to True for SQL query logging
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create base class for models
Base = declarative_base()


def init_db():
    """Initialize the database, creating all tables."""
    from . import models  # Import models to register them

    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session for dependency injection."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session():
    """Get database session for direct use."""
    return SessionLocal()
