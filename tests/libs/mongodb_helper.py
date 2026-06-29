"""
MongoDB helper functions for Robot Framework tests.

Provides direct MongoDB access for verifying audio chunk storage.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

# Load test environment variables
tests_dir = Path(__file__).parent.parent
load_dotenv(tests_dir / ".env.test", override=False)


def get_mongodb_uri():
    """Get MongoDB URI from environment."""
    return os.getenv("MONGODB_URI", "mongodb://localhost:27018")


def get_db_name():
    """Get database name from environment."""
    return os.getenv("TEST_DB_NAME", "test_db")


def get_audio_chunks(conversation_id):
    """
    Get all audio chunks for a conversation from MongoDB.

    Args:
        conversation_id: Conversation ID to query

    Returns:
        List of audio chunk documents (as dictionaries)
    """
    client = MongoClient(get_mongodb_uri())
    db = client[get_db_name()]

    try:
        # Query audio_chunks collection
        chunks = list(
            db.audio_chunks.find(
                {"conversation_id": conversation_id}, sort=[("chunk_index", 1)]
            )
        )

        # Convert ObjectId to string and Binary to bytes length for Robot Framework
        for chunk in chunks:
            if "_id" in chunk:
                chunk["_id"] = str(chunk["_id"])

            # Convert binary audio_data to length (Robot can't handle binary)
            if "audio_data" in chunk:
                chunk["audio_data_length"] = len(chunk["audio_data"])
                # Keep reference but don't pass actual binary data
                chunk["audio_data"] = f"<Binary data: {len(chunk['audio_data'])} bytes>"

        return chunks

    finally:
        client.close()


def get_conversation_chunk_count(conversation_id):
    """
    Get the count of audio chunks for a conversation.

    Args:
        conversation_id: Conversation ID to query

    Returns:
        Number of chunks
    """
    client = MongoClient(get_mongodb_uri())
    db = client[get_db_name()]

    try:
        count = db.audio_chunks.count_documents({"conversation_id": conversation_id})
        return count
    finally:
        client.close()


def verify_chunks_exist(conversation_id, min_chunks=1):
    """
    Verify that audio chunks exist for a conversation.

    Args:
        conversation_id: Conversation ID to verify
        min_chunks: Minimum number of chunks expected

    Returns:
        True if chunks exist and meet minimum count

    Raises:
        AssertionError if chunks don't meet requirements
    """
    chunks = get_audio_chunks(conversation_id)
    actual_count = len(chunks)

    if actual_count < min_chunks:
        raise AssertionError(
            f"Expected at least {min_chunks} chunks, found {actual_count}"
        )

    return True


def _active_transcript_chars(conv):
    """Length of the active transcript version's text (handles list schema)."""
    active = conv.get("active_transcript_version")
    for version in conv.get("transcript_versions") or []:
        if version.get("version_id") == active:
            return len(version.get("transcript") or version.get("text") or "")
    return 0


def find_client_conversations(client_id, include_deleted=True):
    """All conversations for a client (optionally including soft-deleted ones).

    Returns a flat list of dicts with the fields the reconnect/silence e2e asserts
    on: conversation_id, deleted, deletion_reason, end_reason, audio_chunks_count,
    audio_total_duration, and transcript_chars (active version text length). The API
    hides soft-deleted conversations, so these are read straight from Mongo.
    """
    client = MongoClient(get_mongodb_uri())
    db = client[get_db_name()]
    try:
        query = {"client_id": client_id}
        if not include_deleted:
            query["deleted"] = {"$ne": True}
        out = []
        for conv in db.conversations.find(query):
            out.append(
                {
                    "conversation_id": conv.get("conversation_id"),
                    "deleted": bool(conv.get("deleted")),
                    "deletion_reason": conv.get("deletion_reason") or "",
                    "end_reason": conv.get("end_reason") or "",
                    "always_persist": bool(conv.get("always_persist")),
                    "audio_chunks_count": conv.get("audio_chunks_count"),
                    "audio_total_duration": conv.get("audio_total_duration"),
                    "transcript_chars": _active_transcript_chars(conv),
                }
            )
        return out
    finally:
        client.close()


def count_orphaned_transcripts(client_id):
    """Conversations soft-deleted as audio_chunks_not_ready that still carry a
    transcript — the exact data-loss the reconnect fix prevents. Should be 0."""
    return sum(
        1
        for c in find_client_conversations(client_id, include_deleted=True)
        if c["deleted"]
        and c["deletion_reason"] == "audio_chunks_not_ready"
        and c["transcript_chars"] > 0
    )
