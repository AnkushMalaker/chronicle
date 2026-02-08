"""
Plugin routing system for multi-level plugin architecture.

Routes pipeline events to appropriate plugins based on access level and triggers.
"""

import logging
import re
import string
from typing import Dict, List, Optional

from .base import BasePlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)


def normalize_text_for_wake_word(text: str) -> str:
    """
    Normalize text for wake word matching.
    - Lowercase
    - Replace punctuation with spaces
    - Collapse multiple spaces to single space
    - Strip leading/trailing whitespace

    Example:
        "Hey, Vivi!" -> "hey vivi"
        "HEY  VIVI" -> "hey vivi"
        "Hey-Vivi" -> "hey vivi"
    """
    # Lowercase
    text = text.lower()
    # Replace punctuation with spaces (instead of removing, to preserve word boundaries)
    text = text.translate(str.maketrans(string.punctuation, ' ' * len(string.punctuation)))
    # Normalize whitespace (collapse multiple spaces to single space)
    text = re.sub(r'\s+', ' ', text)
    # Strip leading/trailing whitespace
    return text.strip()


def extract_command_after_wake_word(transcript: str, wake_word: str) -> str:
    """
    Intelligently extract command after wake word in original transcript.

    Handles punctuation and spacing variations by creating a flexible regex pattern.

    Example:
        transcript: "Hey, Vivi, turn off lights"
        wake_word: "hey vivi"
        -> extracts: "turn off lights"

    Args:
        transcript: Original transcript text with punctuation
        wake_word: Configured wake word (will be normalized)

    Returns:
        Command text after wake word, or full transcript if wake word boundary not found
    """
    # Split wake word into parts (normalized)
    wake_word_parts = normalize_text_for_wake_word(wake_word).split()

    if not wake_word_parts:
        return transcript.strip()

    # Create regex pattern that allows punctuation/whitespace between parts
    # Example: "hey" + "vivi" -> r"hey[\s,.\-!?]*vivi[\s,.\-!?]*"
    # The pattern matches the wake word parts with optional punctuation/whitespace between and after
    pattern_parts = [re.escape(part) for part in wake_word_parts]
    # Allow optional punctuation/whitespace between parts
    pattern = r'[\s,.\-!?;:]*'.join(pattern_parts)
    # Add trailing punctuation/whitespace consumption after last wake word part
    pattern = '^' + pattern + r'[\s,.\-!?;:]*'

    # Try to match wake word at start of transcript (case-insensitive)
    match = re.match(pattern, transcript, re.IGNORECASE)

    if match:
        # Extract everything after the matched wake word (including trailing punctuation)
        command = transcript[match.end():].strip()
        return command
    else:
        # Fallback: couldn't find wake word boundary, return full transcript
        logger.warning(f"Could not find wake word boundary for '{wake_word}' in '{transcript}', using full transcript")
        return transcript.strip()


class PluginRouter:
    """Routes pipeline events to appropriate plugins based on event subscriptions"""

    def __init__(self):
        self.plugins: Dict[str, BasePlugin] = {}
        # Index plugins by event for fast lookup
        self._plugins_by_event: Dict[str, List[str]] = {}

    def register_plugin(self, plugin_id: str, plugin: BasePlugin):
        """Register a plugin with the router"""
        self.plugins[plugin_id] = plugin

        # Index by each event
        for event in plugin.events:
            if event not in self._plugins_by_event:
                self._plugins_by_event[event] = []
            self._plugins_by_event[event].append(plugin_id)

        logger.info(f"Registered plugin '{plugin_id}' for events: {plugin.events}")

    async def dispatch_event(
        self,
        event: str,
        user_id: str,
        data: Dict,
        metadata: Optional[Dict] = None
    ) -> List[PluginResult]:
        """
        Dispatch event to all subscribed plugins.

        Args:
            event: Event name (e.g., 'transcript.streaming', 'conversation.complete')
            user_id: User ID for context
            data: Event-specific data
            metadata: Optional metadata

        Returns:
            List of plugin results
        """
        # Add at start
        logger.info(f"🔌 ROUTER: Dispatching '{event}' event (user={user_id})")

        results = []

        # Get plugins subscribed to this event
        plugin_ids = self._plugins_by_event.get(event, [])

        # Add subscription check
        if not plugin_ids:
            logger.warning(f"🔌 ROUTER: No plugins subscribed to event '{event}'")
            return results

        logger.info(f"🔌 ROUTER: Found {len(plugin_ids)} subscribed plugin(s): {plugin_ids}")

        for plugin_id in plugin_ids:
            plugin = self.plugins[plugin_id]

            if not plugin.enabled:
                logger.info(f"   ⊘ Skipping '{plugin_id}': disabled")
                continue

            # Check execution condition (wake_word, etc.)
            logger.info(f"   → Checking execution condition for '{plugin_id}'")
            if not await self._should_execute(plugin, data):
                logger.info(f"   ⊘ Skipping '{plugin_id}': condition not met")
                continue

            # Execute plugin
            try:
                logger.info(f"   ▶ Executing '{plugin_id}' for event '{event}'")
                context = PluginContext(
                    user_id=user_id,
                    event=event,
                    data=data,
                    metadata=metadata or {}
                )

                result = await self._execute_plugin(plugin, event, context)

                if result:
                    status_icon = "✓" if result.success else "✗"
                    logger.info(
                        f"   {status_icon} Plugin '{plugin_id}' completed: "
                        f"success={result.success}, message={result.message}"
                    )
                    results.append(result)

                    # If plugin says stop processing, break
                    if not result.should_continue:
                        logger.info(f"   ⊗ Plugin '{plugin_id}' stopped further processing")
                        break

            except Exception as e:
                # CRITICAL: Log exception details
                logger.error(
                    f"   ✗ Plugin '{plugin_id}' FAILED with exception: {e}",
                    exc_info=True
                )

        # Add at end
        logger.info(
            f"🔌 ROUTER: Dispatch complete for '{event}': "
            f"{len(results)} plugin(s) executed successfully"
        )

        return results

    async def _should_execute(self, plugin: BasePlugin, data: Dict) -> bool:
        """Check if plugin should be executed based on condition configuration"""
        condition_type = plugin.condition.get('type', 'always')

        if condition_type == 'always':
            return True

        elif condition_type == 'wake_word':
            # Normalize transcript for matching (handles punctuation and spacing)
            transcript = data.get('transcript', '')
            normalized_transcript = normalize_text_for_wake_word(transcript)

            # Support both singular 'wake_word' and plural 'wake_words' (list)
            wake_words = plugin.condition.get('wake_words', [])
            if not wake_words:
                # Fallback to singular wake_word for backward compatibility
                wake_word = plugin.condition.get('wake_word', '')
                if wake_word:
                    wake_words = [wake_word]

            # Check if transcript starts with any wake word (after normalization)
            for wake_word in wake_words:
                normalized_wake_word = normalize_text_for_wake_word(wake_word)
                if normalized_wake_word and normalized_transcript.startswith(normalized_wake_word):
                    # Smart extraction: find where wake word actually ends in original text
                    command = extract_command_after_wake_word(transcript, wake_word)
                    data['command'] = command
                    data['original_transcript'] = transcript
                    logger.debug(f"Wake word '{wake_word}' detected. Original: '{transcript}', Command: '{command}'")
                    return True

            return False

        elif condition_type == 'conditional':
            # Future: Custom condition checking
            return True

        return False

    async def _execute_plugin(
        self,
        plugin: BasePlugin,
        event: str,
        context: PluginContext
    ) -> Optional[PluginResult]:
        """Execute plugin method for specified event"""
        # Map events to plugin callback methods
        if event.startswith('transcript.'):
            return await plugin.on_transcript(context)
        elif event.startswith('conversation.'):
            return await plugin.on_conversation_complete(context)
        elif event.startswith('memory.'):
            return await plugin.on_memory_processed(context)

        return None

    async def cleanup_all(self):
        """Clean up all registered plugins"""
        for plugin_id, plugin in self.plugins.items():
            try:
                await plugin.cleanup()
                logger.info(f"Cleaned up plugin '{plugin_id}'")
            except Exception as e:
                logger.error(f"Error cleaning up plugin '{plugin_id}': {e}")
