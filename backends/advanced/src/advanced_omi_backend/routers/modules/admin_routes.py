"""
Admin routes for Chronicle API.

Provides admin-only endpoints for system management and cleanup operations.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from advanced_omi_backend.auth import current_active_user
from advanced_omi_backend.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(current_user: User = Depends(current_active_user)) -> User:
    """Dependency to require admin/superuser permissions."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin permissions required")
    return current_user


@router.get("/cleanup/settings")
async def get_cleanup_settings_admin(admin: User = Depends(require_admin)):
    """Get current cleanup settings (admin only)."""
    from advanced_omi_backend.config import get_cleanup_settings

    settings = get_cleanup_settings()
    return {
        **settings,
        "note": "Cleanup settings are stored in /app/data/cleanup_config.json",
    }


@router.post("/cleanup")
async def trigger_cleanup(
    dry_run: bool = Query(False, description="Preview what would be deleted"),
    retention_days: Optional[int] = Query(
        None, description="Override retention period"
    ),
    admin: User = Depends(require_admin),
):
    """Manually trigger cleanup of soft-deleted conversations (admin only)."""
    try:
        from advanced_omi_backend.controllers.queue_controller import get_queue
        from advanced_omi_backend.workers.cleanup_jobs import (
            purge_old_deleted_conversations,
        )

        # Enqueue cleanup job
        queue = get_queue("default")
        job = queue.enqueue(
            purge_old_deleted_conversations,
            retention_days=retention_days,  # Will use config default if None
            dry_run=dry_run,
            job_timeout="30m",
        )

        logger.info(
            f"Admin {admin.email} triggered cleanup job {job.id} (dry_run={dry_run}, retention={retention_days or 'default'})"
        )

        return JSONResponse(
            status_code=200,
            content={
                "message": f"Cleanup job {'(dry run) ' if dry_run else ''}queued successfully",
                "job_id": job.id,
                "retention_days": retention_days or "default (from config)",
                "dry_run": dry_run,
                "note": "Check job status at /api/queue/jobs/{job_id}",
            },
        )

    except Exception as e:
        logger.error(f"Failed to trigger cleanup: {e}")
        return JSONResponse(
            status_code=500, content={"error": f"Failed to trigger cleanup: {str(e)}"}
        )


@router.get("/cleanup/preview")
async def preview_cleanup(
    retention_days: Optional[int] = Query(
        None, description="Preview with specific retention period"
    ),
    admin: User = Depends(require_admin),
):
    """Preview what would be deleted by cleanup (admin only)."""
    try:
        from datetime import datetime, timedelta

        from advanced_omi_backend.config import get_cleanup_settings
        from advanced_omi_backend.models.conversation import Conversation

        # Use provided retention or default from config
        if retention_days is None:
            settings_dict = get_cleanup_settings()
            retention_days = settings_dict["retention_days"]

        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)

        # Count conversations that would be deleted
        count = await Conversation.find(
            Conversation.deleted == True, Conversation.deleted_at < cutoff_date
        ).count()

        return {
            "retention_days": retention_days,
            "cutoff_date": cutoff_date.isoformat(),
            "conversations_to_delete": count,
            "note": f"Conversations deleted before {cutoff_date.date()} would be purged",
        }

    except Exception as e:
        logger.error(f"Failed to preview cleanup: {e}")
        return JSONResponse(
            status_code=500, content={"error": f"Failed to preview cleanup: {str(e)}"}
        )
