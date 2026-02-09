"""
Single source of truth for all plugin event types, button states, and action types.

All event names, button states, and action types live here. No raw strings anywhere else.
Using str, Enum so values work directly as strings in Redis, YAML, JSON — but code
always references the enum member, never a raw string.
"""

from enum import Enum
from typing import Dict


class PluginEvent(str, Enum):
    """All events that can trigger plugins."""

    # Conversation lifecycle
    CONVERSATION_COMPLETE = "conversation.complete"
    TRANSCRIPT_STREAMING = "transcript.streaming"
    TRANSCRIPT_BATCH = "transcript.batch"
    MEMORY_PROCESSED = "memory.processed"

    # Button events (from OMI device)
    BUTTON_SINGLE_PRESS = "button.single_press"
    BUTTON_DOUBLE_PRESS = "button.double_press"

    # Cross-plugin communication (dispatched by PluginServices.call_plugin)
    PLUGIN_ACTION = "plugin_action"


class ButtonState(str, Enum):
    """Raw button states from OMI device firmware."""

    SINGLE_TAP = "SINGLE_TAP"
    DOUBLE_TAP = "DOUBLE_TAP"
    LONG_PRESS = "LONG_PRESS"


# Maps device button states to plugin events
BUTTON_STATE_TO_EVENT: Dict[ButtonState, PluginEvent] = {
    ButtonState.SINGLE_TAP: PluginEvent.BUTTON_SINGLE_PRESS,
    ButtonState.DOUBLE_TAP: PluginEvent.BUTTON_DOUBLE_PRESS,
}


class ButtonActionType(str, Enum):
    """Types of actions a button press can trigger (from test_button_actions plugin config)."""

    CLOSE_CONVERSATION = "close_conversation"
    CALL_PLUGIN = "call_plugin"


class ConversationCloseReason(str, Enum):
    """Reasons for requesting a conversation close."""

    USER_REQUESTED = "user_requested"
    PLUGIN_REQUESTED = "plugin_requested"
    BUTTON_CLOSE = "button_close"
