"""
Base plugin classes for Chronicle multi-level plugin architecture.

Provides:
- PluginContext: Context passed to plugin execution
- PluginResult: Result from plugin execution
- BasePlugin: Abstract base class for all plugins
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from advanced_omi_backend.services.interaction_modes.contracts import (
    InteractionContext,
    InteractionModeDefinition,
    InteractionResult,
)


class PluginConnectivityError(Exception):
    """An external dependency (e.g. a Home Assistant server) is unreachable.

    Raise this from initialize() for transient network conditions. The plugin
    system marks the plugin DEGRADED (not FAILED) and retries initialize() in
    the background with backoff, instead of logging a full traceback and giving
    up until process restart. Reserve plain exceptions for real config/setup
    errors (missing token, bad config) that a retry cannot fix.
    """


@dataclass
class PluginContext:
    """Context passed to plugin execution"""

    user_id: str
    event: str  # Event name (e.g., "transcript.streaming", "conversation.complete")
    data: Dict[str, Any]  # Event-specific data
    metadata: Dict[str, Any] = field(default_factory=dict)
    services: Optional[Any] = (
        None  # PluginServices instance for system/cross-plugin calls
    )


@dataclass
class PluginResult:
    """Result from plugin execution"""

    success: bool
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    should_continue: bool = True  # Whether to continue normal processing


class BasePlugin(ABC):
    """
    Base class for all Chronicle plugins.

    Plugins can hook into different stages of the processing pipeline:
    - transcript: When new transcript segment arrives
    - conversation: When conversation processing completes
    - memory: When memory extraction finishes

    Subclasses should:
    1. Set SUPPORTED_ACCESS_LEVELS to list which levels they support
    2. Implement initialize() for plugin initialization
    3. Implement the appropriate callback methods (on_transcript, on_conversation_complete, on_memory_processed)
    4. Optionally implement cleanup() for resource cleanup
    """

    # Subclasses declare which access levels they support
    SUPPORTED_ACCESS_LEVELS: List[str] = []
    # Operational interaction modes are separate from event subscriptions.  A
    # plugin may own zero or more long-lived modes, each with unique activation
    # phrases registered by PluginRouter.
    INTERACTION_MODES: tuple[InteractionModeDefinition, ...] = ()

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize plugin with configuration.

        Args:
            config: Plugin configuration from config/plugins.yml
                   Contains: enabled, events, condition, and plugin-specific config
        """
        self.config = config
        self.enabled = config.get("enabled", False)
        self.events = config.get("events", [])
        self.condition = config.get("condition", {"type": "always"})
        # Lower runs earlier. Plugins form an ordered chain of responsibility per
        # event: a plugin that handles a command returns should_continue=False to
        # stop the chain; returning None (or should_continue=True) passes it to
        # the next plugin. This is the configurable routing hierarchy (a future
        # drag-to-reorder UI would just edit this value in config/plugins.yml).
        self.priority = config.get("priority", 100)

    def register_prompts(self, registry) -> None:
        """Register plugin prompts with the prompt registry.

        Override to register prompts. Called during plugin discovery,
        before initialize(). Default: no-op (backward-compatible).

        Args:
            registry: PromptRegistry instance
        """
        pass

    @abstractmethod
    async def initialize(self):
        """
        Initialize plugin resources (connect to services, etc.)

        Called during application startup after plugin registration.
        Raise an exception if initialization fails.
        """
        pass

    async def cleanup(self):
        """
        Clean up plugin resources.

        Called during application shutdown.
        Override if your plugin needs cleanup (closing connections, etc.)
        """
        pass

    async def health_check(self) -> Dict[str, Any]:
        """
        Live connectivity check using initialized clients.

        Override in plugins that connect to external services.
        Returns dict with at least 'ok' (bool) and 'message' (str).
        Optionally includes 'latency_ms' (int).
        """
        return {"ok": True, "message": "No external service to check"}

    # Access-level specific methods (implement only what you need)

    async def on_transcript(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Called when new transcript segment arrives.

        Context data contains:
            - transcript: str - The transcript text
            - segment_id: str - Unique segment identifier
            - conversation_id: str - Current conversation ID

        For wake_word conditions, router adds:
            - command: str - Command with wake word stripped
            - original_transcript: str - Full transcript

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_conversation_complete(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Called when conversation processing completes.

        Context data contains:
            - conversation: dict - Full conversation data
            - transcript: str - Complete transcript
            - duration: float - Conversation duration
            - conversation_id: str - Conversation identifier

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_memory_processed(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Called after memory extraction finishes.

        Context data contains:
            - memories: list - Extracted memories
            - conversation: dict - Source conversation
            - memory_count: int - Number of memories created
            - conversation_id: str - Conversation identifier

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_conversation_starred(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Called when a conversation is starred or unstarred.

        Context data contains:
            - conversation_id: str - Conversation identifier
            - starred: bool - New starred state (True = starred, False = unstarred)
            - starred_at: str or None - ISO timestamp when starred (None if unstarred)
            - title: str or None - Conversation title

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_button_event(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Called when a device button event is received.

        Context data contains:
            - state: str - Button state (e.g., "SINGLE_PRESS", "DOUBLE_PRESS", "LONG_PRESS")
            - timestamp: float - Unix timestamp of the event
            - audio_uuid: str - Current audio session UUID (may be None)

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_wake_word_detected(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Called when the standalone wakeword-service detects the acoustic wake word
        and captures the command turn.

        Context data contains:
            - command: str - The captured command text (resolved from existing
              transcription; may be empty if transcription lagged)
            - client_id: str - Client identifier
            - session_id: str - Audio session id
            - conversation_id: str or None - Current conversation id (if any)
            - score: float - Acoustic detection score
            - reason: str - End-of-turn reason ("smart_turn" | "max_duration")

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_plugin_action(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Called when another plugin dispatches an action to this plugin via PluginServices.call_plugin().

        Context data contains:
            - action: str - Action name (e.g., "toggle_lights", "call_service")
            - Plus any additional data from the calling plugin

        Returns:
            PluginResult with success status, optional message, and should_continue flag
        """
        pass

    async def on_interaction_start(
        self, context: InteractionContext
    ) -> Optional[InteractionResult]:
        """Start one of this plugin's registered interaction modes."""
        pass

    async def on_interaction_turn(
        self, context: InteractionContext
    ) -> Optional[InteractionResult]:
        """Handle an utterance while one of this plugin's modes is active."""
        pass

    async def on_interaction_end(
        self, context: InteractionContext
    ) -> Optional[InteractionResult]:
        """Observe mode completion or expiry and release plugin resources."""
        pass
