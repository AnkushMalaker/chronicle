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

        Only succeeds when open_conversation_job is actively running and polling
        (indicated by the conversation:current:{session_id} Redis key). During
        speech detection phase, no conversation is open — the flag would go unread.

        Args:
            session_id: The streaming session ID (typically same as client_id)
            reason: Why the conversation is being closed

        Returns:
            True if the close request was set successfully, False if no
            conversation is currently open for this session
        """
        # Gate: only set the flag when open_conversation_job is running and will read it.
        # The conversation:current key is set right before the polling loop starts.
        conversation_id = await self._async_redis.get(
            f"conversation:current:{session_id}"
        )
        if not conversation_id:
            logger.warning(
                f"No open conversation for session {session_id} — close request ignored"
            )
            return False

        from advanced_omi_backend.controllers.session_controller import (
            request_conversation_close,
        )

        return await request_conversation_close(
            self._async_redis, session_id, reason=reason.value
        )

    async def star_conversation(self, session_id: str) -> bool:
        """Toggle the star on the current conversation for a session.

        Looks up the current conversation from Redis and calls toggle_star().

        Args:
            session_id: The streaming session ID

        Returns:
            True if the star toggle was successful
        """
        from advanced_omi_backend.controllers.conversation_controller import toggle_star
        from advanced_omi_backend.models.conversation import Conversation
        from advanced_omi_backend.users import User

        # Look up current conversation_id from Redis
        conversation_id = await self._async_redis.get(
            f"conversation:current:{session_id}"
        )
        if not conversation_id:
            logger.warning(f"No current conversation for session {session_id}")
            return False

        # Find conversation to get user_id
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        if not conversation:
            logger.warning(f"Conversation {conversation_id} not found for starring")
            return False

        # Look up user
        user = await User.get(conversation.user_id)
        if not user:
            logger.warning(f"User {conversation.user_id} not found for starring")
            return False

        result = await toggle_star(conversation_id, user)
        # toggle_star returns a dict on success, JSONResponse on error
        return isinstance(result, dict) and "starred" in result

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
            return PluginResult(
                success=False, message=f"Plugin '{plugin_id}' not found"
            )
        if not plugin.enabled:
            logger.warning(f"Plugin '{plugin_id}' is disabled, cannot call")
            return PluginResult(
                success=False, message=f"Plugin '{plugin_id}' is disabled"
            )

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
            logger.error(
                f"Cross-plugin call to {plugin_id}.{action} failed: {e}", exc_info=True
            )
            return PluginResult(success=False, message=f"Plugin action failed: {e}")
