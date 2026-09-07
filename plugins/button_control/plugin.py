"""
Button Configuration plugin — maps device button events to configurable actions.

Each button event (single / double press) is mapped, in config.yml, to one typed
action:

  - stop_playback      : stop the TTS currently playing on the device (barge-in)
  - close_conversation : end the current conversation (triggers post-processing)
  - star_conversation  : star/unstar the current conversation
  - call_plugin        : dispatch an action to another plugin (e.g. Home Assistant)

Actions are validated against the ``ButtonActionType`` enum so a typo in config
fails loudly instead of silently doing nothing.
"""

import logging
from typing import Any, Dict, List, Optional

from backend.plugins.base import BasePlugin, PluginContext, PluginResult
from backend.plugins.events import (
    ButtonActionType,
    ConversationCloseReason,
    PluginEvent,
)

logger = logging.getLogger(__name__)


class ButtonControlPlugin(BasePlugin):
    """Maps button press events to configurable system actions."""

    SUPPORTED_ACCESS_LEVELS: List[str] = ["button"]

    name = "Button Configuration"
    description = (
        "Map device button presses to actions (stop playback, close/star "
        "conversation, call another plugin)."
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.actions = config.get("actions", {})

    async def initialize(self):
        if not self.enabled:
            logger.info("Button Configuration plugin is disabled, skipping init")
            return
        logger.info(
            f"Button Configuration plugin initialized with actions: "
            f"{ {k: v.get('type') for k, v in self.actions.items()} }"
        )

    async def on_button_event(self, context: PluginContext) -> Optional[PluginResult]:
        """Handle a button event by dispatching its configured action."""
        event = context.event

        if event == PluginEvent.BUTTON_SINGLE_PRESS:
            action_key = "single_press"
        elif event == PluginEvent.BUTTON_DOUBLE_PRESS:
            action_key = "double_press"
        else:
            logger.debug(f"No action mapping for event: {event}")
            return None

        action_config = self.actions.get(action_key)
        if not action_config:
            logger.debug(f"No action configured for {action_key}")
            return None

        try:
            action_type = ButtonActionType(action_config.get("type", ""))
        except ValueError:
            logger.warning(f"Unknown button action type: {action_config.get('type')}")
            return PluginResult(
                success=False,
                message=f"Unknown action type: {action_config.get('type')}",
            )

        if action_type == ButtonActionType.STOP_PLAYBACK:
            return await self._handle_stop_playback(context)
        if action_type == ButtonActionType.CLOSE_CONVERSATION:
            return await self._handle_close_conversation(context)
        if action_type == ButtonActionType.STAR_CONVERSATION:
            return await self._handle_star_conversation(context)
        if action_type == ButtonActionType.CALL_PLUGIN:
            return await self._handle_call_plugin(context, action_config)

        return None

    async def _handle_stop_playback(self, context: PluginContext) -> PluginResult:
        """Stop the TTS currently playing on the device that sent the button event."""
        if not context.services:
            logger.error("PluginServices not available in context")
            return PluginResult(success=False, message="Services not available")

        client_id = context.data.get("client_id")
        if not client_id:
            logger.warning("No client_id in button event data, cannot stop playback")
            return PluginResult(success=False, message="No device for this event")

        await context.services.stop_playback(context.user_id, client_id)
        logger.info(f"Button press stopped playback on {client_id}")
        return PluginResult(
            success=True,
            message="Playback stopped by button press",
            should_continue=False,
        )

    async def _handle_close_conversation(self, context: PluginContext) -> PluginResult:
        """Close the current conversation via PluginServices."""
        if not context.services:
            logger.error("PluginServices not available in context")
            return PluginResult(success=False, message="Services not available")

        session_id = context.data.get("session_id")
        if not session_id:
            logger.warning("No session_id in button event data, cannot close")
            return PluginResult(success=False, message="No active session")

        success = await context.services.close_conversation(
            session_id=session_id,
            reason=ConversationCloseReason.BUTTON_CLOSE,
        )

        if success:
            logger.info(f"Button press closed conversation for {session_id[:12]}")
            return PluginResult(
                success=True,
                message="Conversation closed by button press",
                should_continue=False,
            )
        logger.warning(f"Failed to close conversation for {session_id[:12]}")
        return PluginResult(success=False, message="Failed to close conversation")

    async def _handle_star_conversation(self, context: PluginContext) -> PluginResult:
        """Star/unstar the current conversation via PluginServices."""
        if not context.services:
            logger.error("PluginServices not available in context")
            return PluginResult(success=False, message="Services not available")

        session_id = context.data.get("session_id")
        if not session_id:
            logger.warning("No session_id in button event data, cannot star")
            return PluginResult(success=False, message="No active session")

        success = await context.services.star_conversation(session_id=session_id)

        if success:
            logger.info(f"Button press toggled star for {session_id[:12]}")
            return PluginResult(
                success=True, message="Conversation star toggled by button press"
            )
        logger.warning(f"Failed to toggle star for {session_id[:12]}")
        return PluginResult(success=False, message="Failed to toggle conversation star")

    async def _handle_call_plugin(
        self, context: PluginContext, action_config: dict
    ) -> PluginResult:
        """Dispatch an action to another plugin via PluginServices."""
        if not context.services:
            logger.error("PluginServices not available in context")
            return PluginResult(success=False, message="Services not available")

        plugin_id = action_config.get("plugin_id")
        action = action_config.get("action")
        data = action_config.get("data", {})

        if not plugin_id or not action:
            logger.warning(
                f"call_plugin action missing plugin_id or action: {action_config}"
            )
            return PluginResult(
                success=False, message="Invalid call_plugin configuration"
            )

        result = await context.services.call_plugin(
            plugin_id=plugin_id,
            action=action,
            data=data,
            user_id=context.user_id,
        )

        if result:
            return result
        return PluginResult(
            success=False, message=f"No response from plugin '{plugin_id}'"
        )
