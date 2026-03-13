"""
Chronicle plugin system for multi-level pipeline extension.

Plugins can hook into different stages of the processing pipeline:
- transcript: When new transcript segment arrives
- conversation: When conversation processing completes
- memory: After memory extraction finishes
- button: When device button events are received
- plugin_action: Cross-plugin communication

Trigger types control when plugins execute:
- wake_word: Only when transcript starts with specified wake word
- always: Execute on every invocation at access level
- conditional: Execute based on custom condition (future)
"""

from .base import BasePlugin, PluginContext, PluginResult
from .events import ButtonActionType, ButtonState, ConversationCloseReason, PluginEvent
from .router import PluginRouter
from .services import PluginServices

__all__ = [
    "BasePlugin",
    "ButtonActionType",
    "ButtonState",
    "ConversationCloseReason",
    "PluginContext",
    "PluginEvent",
    "PluginResult",
    "PluginRouter",
    "PluginServices",
]
