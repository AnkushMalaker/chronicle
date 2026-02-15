"""
PluginServices — typed interface for plugin-to-system and plugin-to-plugin communication.

Plugins use this interface (via context.services) to interact with the core system
(e.g., close a conversation) or with other plugins (e.g., call Home Assistant to toggle lights).
"""

import logging
from typing import TYPE_CHECKING, Optional

import redis.asyncio as aioredis


from .base import PluginContext, PluginResult
from .events import ConversationCloseReason, PluginEvent

if TYPE_CHECKING:
    from .router import PluginRouter

logger = logging.getLogger(__name__)


class PluginServices:
    """Typed interface for plugin-to-system and plugin-to-plugin communication."""

    def __init__(self, router: "PluginRouter", redis_url: str):
        self._router = router
        self._async_redis = aioredis.from_url(redis_url, decode_responses=True)

    async def cleanup(self):
        """Close the shared async Redis connection pool."""
        try:
            await self._async_redis.aclose()
        except Exception as e:
            logger.debug(f"Error closing async Redis pool: {e}")

    async def close_conversation(
        self,
        session_id: str,
        reason: ConversationCloseReason = ConversationCloseReason.PLUGIN_REQUESTED,
    ) -> bool:
        """Request closing the current conversation for a session.

        Signals the open_conversation_job to close the current conversation
        and trigger post-processing. The session stays active for new conversations.

        Args:
            session_id: The streaming session ID (typically same as client_id)
            reason: Why the conversation is being closed

        Returns:
            True if the close request was set successfully
        """
        from advanced_omi_backend.controllers.session_controller import (
            request_conversation_close,
        )

        return await request_conversation_close(self._async_redis, session_id, reason=reason.value)

    async def call_plugin(
        self,
        plugin_id: str,
        action: str,
        data: dict,
        user_id: str = "system",
    ) -> Optional[PluginResult]:
        """Dispatch an action to another plugin's on_plugin_action() handler.

        Args:
            plugin_id: Target plugin identifier (e.g., "homeassistant")
            action: Action name (e.g., "toggle_lights")
            data: Action-specific data
            user_id: User context for the action

        Returns:
            PluginResult from the target plugin, or error result if plugin not found
        """
        plugin = self._router.plugins.get(plugin_id)
        if not plugin:
            logger.warning(f"Plugin '{plugin_id}' not found for cross-plugin call")
            return PluginResult(success=False, message=f"Plugin '{plugin_id}' not found")
        if not plugin.enabled:
            logger.warning(f"Plugin '{plugin_id}' is disabled, cannot call")
            return PluginResult(success=False, message=f"Plugin '{plugin_id}' is disabled")

        context = PluginContext(
            user_id=user_id,
            event=PluginEvent.PLUGIN_ACTION,
            data={**data, "action": action},
            services=self,
        )

        try:
            result = await plugin.on_plugin_action(context)
            if result:
                logger.info(
                    f"Cross-plugin call {plugin_id}.{action}: "
                    f"success={result.success}, message={result.message}"
                )
            return result
        except Exception as e:
            logger.error(f"Cross-plugin call to {plugin_id}.{action} failed: {e}", exc_info=True)
            return PluginResult(success=False, message=f"Plugin action failed: {e}")
