"""
PluginServices — typed interface for plugin-to-system and plugin-to-plugin communication.

Plugins use this interface (via context.services) to interact with the core system
(e.g., close a conversation) or with other plugins (e.g., call Home Assistant to toggle lights).
"""

import json
import logging
from typing import TYPE_CHECKING, Optional

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.redis_factory import create_async_redis
from advanced_omi_backend.redis_keys import ClientId, device_downlink_channel
from advanced_omi_backend.services.audio_stream.session_store import SessionStore
from advanced_omi_backend.users import User

from .base import PluginContext, PluginResult
from .events import ConversationCloseReason, PluginEvent

if TYPE_CHECKING:
    from .router import PluginRouter

logger = logging.getLogger(__name__)


class PluginServices:
    """Typed interface for plugin-to-system and plugin-to-plugin communication."""

    def __init__(self, router: "PluginRouter"):
        self._router = router
        self._async_redis = create_async_redis(decode_responses=True)

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

        Only succeeds when ``open_conversation_job`` has registered an active semantic
        Conversation in the capture session state. During speech detection no
        Conversation is open, so a close request would go unread.

        Args:
            session_id: The immutable streaming recording-session ID
            reason: Why the conversation is being closed

        Returns:
            True if the close request was set successfully, False if no
            conversation is currently open for this session
        """
        # Gate on the typed session-state pointer set immediately before monitoring.
        conversation_id = await SessionStore(
            self._async_redis
        ).get_active_conversation_id(session_id)
        if not conversation_id:
            logger.warning(
                f"No open conversation for session {session_id} — close request ignored"
            )
            return False

        return await SessionStore(self._async_redis).request_close(
            session_id, reason.value
        )

    async def star_conversation(self, session_id: str) -> bool:
        """Toggle the star on the current conversation for a session.

        Looks up the current conversation from Redis and calls toggle_star().

        Args:
            session_id: The streaming session ID

        Returns:
            True if the star toggle was successful
        """
        # Lazy import: circular dependency (conversation_controller imports
        # plugin_service, which imports this module)
        from advanced_omi_backend.controllers.conversation_controller import toggle_star

        # Look up current conversation_id from Redis
        conversation_id = await SessionStore(
            self._async_redis
        ).get_active_conversation_id(session_id)
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

    async def stop_playback(self, client_id: str) -> bool:
        """Stop any TTS currently playing on a device (barge-in).

        Publishes a ``stop-audio`` control frame to the device's downlink channel.
        The WebSocket handler that owns the device connection picks it up and, for
        Opus-streaming clients, cancels the in-flight stream and tells the device to
        flush (see ``device_audio.stop_play_audio``). Decoupled via Redis so this
        works from any process (the button handler runs in the backend, but wake
        handlers run in the workers).

        Args:
            client_id: The device/client whose playback should stop.

        Returns:
            True if the stop request was published.
        """
        if not client_id:
            logger.warning("stop_playback called with no client_id")
            return False
        message = json.dumps({"type": "stop-audio", "data": {}})
        client_ref = ClientId.from_value(client_id)
        await self._async_redis.publish(
            str(device_downlink_channel(client_ref)), message
        )
        logger.info(f"⏹ Requested stop-audio for {client_id}")
        return True

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
