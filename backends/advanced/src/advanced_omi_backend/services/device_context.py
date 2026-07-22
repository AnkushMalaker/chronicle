"""Create bounded context requests for completed conversations."""

from datetime import timedelta

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.device_input import (
    CaptureSource,
    DeviceInputItem,
    DeviceInputJob,
)


async def request_conversation_context_jobs(conversation: Conversation) -> list[str]:
    margin = timedelta(minutes=5)
    start_at = conversation.created_at - margin
    end_at = (
        conversation.created_at
        + timedelta(seconds=conversation.audio_total_duration or 0)
        + margin
    )
    sources = await CaptureSource.find(
        CaptureSource.user_id == conversation.user_id,
        CaptureSource.provider == "screenpipe",
    ).to_list()
    # Immich discovery is asynchronous; link metadata-only candidates already
    # known for the same bounded interval without downloading their pixels.
    immich_items = await DeviceInputItem.find(
        DeviceInputItem.user_id == conversation.user_id,
        DeviceInputItem.kind == "immich_memory",
        DeviceInputItem.captured_at >= start_at,
        DeviceInputItem.captured_at <= end_at,
        DeviceInputItem.conversation_id == None,  # noqa: E711
    ).to_list()
    for item in immich_items:
        item.conversation_id = conversation.conversation_id
        if item.state != "promoted":
            item.state = "linked"
        await item.save()
    jobs: list[str] = []
    for source in sources:
        existing = await DeviceInputJob.find_one(
            {
                "source_id": source.source_id,
                "purpose": "conversation_enrichment",
                "payload.conversation_id": conversation.conversation_id,
                "status": {"$in": ["pending", "claimed", "complete"]},
            }
        )
        if existing:
            jobs.append(str(existing.id))
            continue
        job = DeviceInputJob(
            user_id=conversation.user_id,
            source_id=source.source_id,
            kind="screen_context",
            start_at=start_at,
            end_at=end_at,
            purpose="conversation_enrichment",
            payload={"conversation_id": conversation.conversation_id},
        )
        await job.insert()
        jobs.append(str(job.id))
    return jobs
