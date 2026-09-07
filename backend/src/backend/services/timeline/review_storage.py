"""Fail closed before connecting the selection workflow to incompatible review state."""


async def assert_memory_review_storage_ready(database) -> None:
    collection = database["memory_review_proposals"]
    old = await collection.count_documents(
        {
            "$or": [
                {"request_id": {"$exists": False}},
                {"selected_episodes": {"$exists": False}},
                {"selection_hash": {"$exists": False}},
            ]
        },
        limit=1,
    )
    indexes = await collection.index_information()
    if old or "memory_review_day_generation" in indexes:
        raise RuntimeError(
            "Selective memory review requires an explicit cutover: archive and verify "
            "existing proposal decisions, review their episode mapping, and replace "
            "the memory_review_day_generation index. No review state was converted."
        )
