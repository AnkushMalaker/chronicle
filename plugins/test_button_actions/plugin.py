"""
Test Button Actions plugin — maps device button events to configurable actions.

Single press: close conversation (triggers post-processing pipeline)
Double press: cross-plugin call (e.g., toggle study lights via Home Assistant)

Actions are configured in config.yml with typed enums for safety.
"""

import logging
from typing import Any, Dict, List, Optional

from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult
from advanced_omi_backend.plugins.events import ButtonActionType, ConversationCloseReason, PluginEvent

logger = logging.getLogger(__name__)


class TestButtonActionsPlugin(BasePlugin):
    """Maps button press events to configurable system actions."""

    SUPPORTED_ACCESS_LEVELS: List[str] = ["button"]

    name = "Test Button Actions"
    description = "Map OMI device button presses to actions (close conversation, toggle lights, etc.)"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.actions = config.get("actions", {})

    async def initialize(self):
        if not self.enabled:
            logger.info("Test Button Actions plugin is disabled, skipping initialization")
            return
        logger.info(
            f"Test Button Actions plugin initialized with actions: "
            f"{list(self.actions.keys())}"
        )

    async def on_button_event(self, context: PluginContext) -> Optional[PluginResult]:
        """Handle button events by dispatching configured actions."""
        event = context.event

        # Map plugin event to action config key
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
            logger.warning(f"Unknown action type: {action_config.get('type')}")
            return PluginResult(
                success=False,
                message=f"Unknown action type: {action_config.get('type')}",
            )

        if action_type == ButtonActionType.CLOSE_CONVERSATION:
            return await self._handle_close_conversation(context, action_config)
        elif action_type == ButtonActionType.CALL_PLUGIN:
            return await self._handle_call_plugin(context, action_config)

        return None

    async def _handle_close_conversation(
        self, context: PluginContext, action_config: dict
    ) -> PluginResult:
        """Close the current conversation via PluginServices."""
        if not context.services:
            logger.error("PluginServices not available in context")
            return PluginResult(success=False, message="Services not available")

        session_id = context.data.get("session_id")
        if not session_id:
            logger.warning("No session_id in button event data, cannot close conversation")
            return PluginResult(success=False, message="No active session")

        success = await context.services.close_conversation(
            session_id=session_id,
            reason=ConversationCloseReason.BUTTON_CLOSE,
        )

        if success:
            logger.info(f"Button press closed conversation for session {session_id[:12]}")
            return PluginResult(
                success=True,
                message="Conversation closed by button press",
                should_continue=False,
            )
        else:
            logger.warning(f"Failed to close conversation for session {session_id[:12]}")
            return PluginResult(success=False, message="Failed to close conversation")

    async def _handle_call_plugin(
        self, context: PluginContext, action_config: dict
    ) -> PluginResult:
        """Dispatch action to another plugin via PluginServices."""
        if not context.services:
            logger.error("PluginServices not available in context")
            return PluginResult(success=False, message="Services not available")

        plugin_id = action_config.get("plugin_id")
        action = action_config.get("action")
        data = action_config.get("data", {})

        if not plugin_id or not action:
            logger.warning(f"call_plugin action missing plugin_id or action: {action_config}")
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

        return PluginResult(success=False, message=f"No response from plugin '{plugin_id}'")
