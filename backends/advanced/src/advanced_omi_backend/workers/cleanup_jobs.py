"""
Cleanup jobs for managing soft-deleted data.

Provides manual cleanup of soft-deleted conversations and chunks.
Auto-cleanup is controlled via admin API settings (stored in /app/data/cleanup_config.json).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from advanced_omi_backend.config import CleanupSettings, get_cleanup_settings
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.job import async_job
from advanced_omi_backend.models.waveform import WaveformData

logger = logging.getLogger(__name__)


@async_job(redis=False, beanie=True, timeout=1800)  # 30 minute timeout
async def purge_old_deleted_conversations(
    retention_days: Optional[int] = None,
    dry_run: bool = False
) -> dict:
    """
    Permanently delete conversations that have been soft-deleted for longer than retention period.

    Args:
        retention_days: Number of days to keep soft-deleted conversations (defaults to config value)
        dry_run: If True, only count what would be deleted without actually deleting

    Returns:
        Dict with counts of purged conversations, chunks, and waveforms
    """
    # Get retention period from config if not specified
    if retention_days is None:
        settings_dict = get_cleanup_settings()
        retention_days = settings_dict['retention_days']

    cutoff_date = datetime.utcnow() - timedelta(days=retention_days)

    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Purging conversations deleted before {cutoff_date.isoformat()}")

    # Find soft-deleted conversations older than cutoff
    old_deleted = await Conversation.find(
        Conversation.deleted == True,
        Conversation.deleted_at < cutoff_date
    ).to_list()

    purged_conversations = 0
    purged_chunks = 0
    purged_waveforms = 0

    for conversation in old_deleted:
        conversation_id = conversation.conversation_id

        if not dry_run:
            # Hard delete chunks
            chunk_result = await AudioChunkDocument.find(
                AudioChunkDocument.conversation_id == conversation_id
            ).delete()
            purged_chunks += chunk_result.deleted_count

            # Hard delete waveforms
            waveform_result = await WaveformData.find(
                WaveformData.conversation_id == conversation_id
            ).delete()
            purged_waveforms += waveform_result.deleted_count

            # Hard delete conversation
            await conversation.delete()
            purged_conversations += 1

            logger.info(
                f"Purged conversation {conversation_id} "
                f"(deleted {chunk_result.deleted_count} chunks, "
                f"{waveform_result.deleted_count} waveforms)"
            )
        else:
            # Dry run - just count
            chunk_count = await AudioChunkDocument.find(
                AudioChunkDocument.conversation_id == conversation_id
            ).count()
            purged_chunks += chunk_count

            waveform_count = await WaveformData.find(
                WaveformData.conversation_id == conversation_id
            ).count()
            purged_waveforms += waveform_count

            purged_conversations += 1

            logger.info(
                f"[DRY RUN] Would purge conversation {conversation_id} "
                f"(with {chunk_count} chunks, {waveform_count} waveforms)"
            )

    logger.info(
        f"{'[DRY RUN] Would purge' if dry_run else 'Purged'} "
        f"{purged_conversations} conversations, {purged_chunks} chunks, "
        f"and {purged_waveforms} waveforms"
    )

    return {
        "purged_conversations": purged_conversations,
        "purged_chunks": purged_chunks,
        "purged_waveforms": purged_waveforms,
        "retention_days": retention_days,
        "cutoff_date": cutoff_date.isoformat(),
        "dry_run": dry_run,
    }


def schedule_cleanup_job(retention_days: Optional[int] = None) -> Optional[str]:
    """
    Enqueue cleanup job to run once (manual trigger or scheduled task).

    This function only schedules the job if auto-cleanup is enabled via
    admin API settings (stored in /app/data/cleanup_config.json).

    For manual cleanup, use the admin API endpoint: POST /api/admin/cleanup

    Args:
        retention_days: Number of days to keep soft-deleted conversations
                       (defaults to config value)

    Returns:
        Job ID if scheduled successfully, None otherwise
    """
    # Check if auto-cleanup is enabled
    settings_dict = get_cleanup_settings()
    if not settings_dict['auto_cleanup_enabled']:
        logger.info("Auto-cleanup is disabled (auto_cleanup_enabled=false)")
        return None

    try:
        from advanced_omi_backend.controllers.queue_controller import get_queue

        if retention_days is None:
            retention_days = settings_dict['retention_days']

        queue = get_queue("default")
        job = queue.enqueue(
            purge_old_deleted_conversations,
            retention_days=retention_days,
            dry_run=False,
            job_timeout="30m",
        )
        logger.info(f"Scheduled cleanup job {job.id} with {retention_days}-day retention")
        return job.id

    except Exception as e:
        logger.error(f"Failed to schedule cleanup job: {e}")
        return None

