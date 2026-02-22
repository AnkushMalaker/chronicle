"""
Single source of truth for all plugin event types, button states, and action types.

All event names, button states, and action types live here. No raw strings anywhere else.
Using str, Enum so values work directly as strings in Redis, YAML, JSON — but code
always references the enum member, never a raw string.
"""

from enum import Enum
from typing import Dict  # Used by BUTTON_STATE_TO_EVENT


class PluginEvent(str, Enum):
    """All events that can trigger plugins.

    Each member carries a human-readable ``description`` attribute so event
    metadata stays in sync with the enum automatically.
    """

    def __new__(cls, value: str, description: str = ""):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    # Conversation lifecycle
    CONVERSATION_COMPLETE = (
        "conversation.complete",
        "Fires when conversation processing finishes (transcript ready)",
    )
    TRANSCRIPT_STREAMING = (
        "transcript.streaming",
        "Real-time transcript segments during a live conversation",
    )
    TRANSCRIPT_BATCH = (
        "transcript.batch",
        "Batch transcript from file upload processing",
    )
    MEMORY_PROCESSED = (
        "memory.processed",
        "After memories are extracted from a conversation",
    )
    CONVERSATION_STARRED = (
        "conversation.starred",
        "Fires when a conversation is starred or unstarred",
    )

    # Button events (from OMI device)
    BUTTON_SINGLE_PRESS = ("button.single_press", "OMI device button single press")
    BUTTON_DOUBLE_PRESS = ("button.double_press", "OMI device button double press")

    # Cross-plugin communication (dispatched by PluginServices.call_plugin)
    PLUGIN_ACTION = (
        "plugin_action",
        "Cross-plugin dispatch via PluginServices.call_plugin()",
    )


class ButtonState(str, Enum):
    """Raw button states from OMI device firmware."""

    SINGLE_PRESS = "SINGLE_PRESS"
    DOUBLE_PRESS = "DOUBLE_PRESS"
    LONG_PRESS = "LONG_PRESS"


# Maps device button states to plugin events
BUTTON_STATE_TO_EVENT: Dict[ButtonState, PluginEvent] = {
    ButtonState.SINGLE_PRESS: PluginEvent.BUTTON_SINGLE_PRESS,
    ButtonState.DOUBLE_PRESS: PluginEvent.BUTTON_DOUBLE_PRESS,
}


class ButtonActionType(str, Enum):
    """Types of actions a button press can trigger (from test_button_actions plugin config)."""

    CLOSE_CONVERSATION = "close_conversation"
    STAR_CONVERSATION = "star_conversation"
    CALL_PLUGIN = "call_plugin"


class ConversationCloseReason(str, Enum):
    """Reasons for requesting a conversation close."""

    USER_REQUESTED = "user_requested"
    PLUGIN_REQUESTED = "plugin_requested"
    BUTTON_CLOSE = "button_close"
