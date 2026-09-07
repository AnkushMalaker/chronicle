"""Canonical Mongo query fragments for Main and isolated-space material."""

from typing import Optional


def conversation_scope_filter(memory_space_id: Optional[str] = None) -> dict:
    if memory_space_id:
        return {"memory_space_id": memory_space_id}
    # Mongo's equality-to-null semantics include pre-existing documents where the
    # field is absent. That is the canonical Main representation, not a legacy path.
    return {
        "$or": [
            {"memory_space_id": None},
            {"published_to_main_at": {"$ne": None}},
        ]
    }


def main_only_filter() -> dict:
    return {"memory_space_id": None}
