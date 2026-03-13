"""
Test-only API routes for integration testing.

These routes are ONLY loaded when DEBUG_DIR environment variable is set,
which happens in test environments. They should never be available in production.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from advanced_omi_backend.services.plugin_service import get_plugin_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/test", tags=["testing"])


@router.delete("/plugins/events")
async def clear_test_plugin_events():
    """
    Clear all test plugin events.

    This endpoint is ONLY available in test environments and provides a clean
    way to reset plugin event state between tests without direct database access.

    Returns:
        dict: Confirmation message with number of events cleared
    """
    plugin_router = get_plugin_router()

    if not plugin_router:
        return {"message": "No plugin router initialized", "events_cleared": 0}

    total_cleared = 0

    # Clear events from all plugins that have storage
    for plugin_id, plugin in plugin_router.plugins.items():
        if hasattr(plugin, "storage") and plugin.storage:
            try:
                cleared = await plugin.storage.clear_events()
                total_cleared += cleared
                logger.info(f"Cleared {cleared} events from plugin '{plugin_id}'")
            except Exception as e:
                logger.error(f"Error clearing events from plugin '{plugin_id}': {e}")

    return {"message": "Test plugin events cleared", "events_cleared": total_cleared}


@router.get("/plugins/events/count")
async def get_test_plugin_event_count(event_type: Optional[str] = None):
    """
    Get count of test plugin events.

    Args:
        event_type: Optional event type to filter by (e.g., 'transcript.batch')

    Returns:
        dict: Event count and event type filter
    """
    plugin_router = get_plugin_router()

    if not plugin_router:
        return {
            "count": 0,
            "event_type": event_type,
            "message": "No plugin router initialized",
        }

    # Get count from first plugin with storage (usually test_event plugin)
    for plugin_id, plugin in plugin_router.plugins.items():
        if hasattr(plugin, "storage") and plugin.storage:
            try:
                count = await plugin.storage.get_event_count(event_type)
                return {
                    "count": count,
                    "event_type": event_type,
                    "plugin_id": plugin_id,
                }
            except Exception as e:
                logger.error(
                    f"Error getting event count from plugin '{plugin_id}': {e}"
                )
                raise HTTPException(status_code=500, detail=str(e))

    return {
        "count": 0,
        "event_type": event_type,
        "message": "No plugin with storage found",
    }


@router.get("/plugins/events")
async def get_test_plugin_events(event_type: Optional[str] = None):
    """
    Get test plugin events.

    Args:
        event_type: Optional event type to filter by

    Returns:
        dict: List of events
    """
    plugin_router = get_plugin_router()

    if not plugin_router:
        return {"events": [], "message": "No plugin router initialized"}

    # Get events from first plugin with storage
    for plugin_id, plugin in plugin_router.plugins.items():
        if hasattr(plugin, "storage") and plugin.storage:
            try:
                if event_type:
                    events = await plugin.storage.get_events_by_type(event_type)
                else:
                    events = await plugin.storage.get_all_events()

                return {
                    "events": events,
                    "count": len(events),
                    "event_type": event_type,
                    "plugin_id": plugin_id,
                }
            except Exception as e:
                logger.error(f"Error getting events from plugin '{plugin_id}': {e}")
                raise HTTPException(status_code=500, detail=str(e))

    return {"events": [], "message": "No plugin with storage found"}
