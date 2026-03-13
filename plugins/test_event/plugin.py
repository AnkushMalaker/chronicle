"""
Test Event Plugin

Logs all plugin events to SQLite database for integration testing.
Subscribes to all event types to verify event dispatch system works correctly.
"""

import logging
from typing import Any, Dict, List, Optional

from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

from .event_storage import EventStorage

logger = logging.getLogger(__name__)


class TestEventPlugin(BasePlugin):
    """
    Test plugin that logs all events for verification.

    Subscribes to:
    - transcript.streaming: Real-time WebSocket transcription
    - transcript.batch: File upload batch transcription
    - conversation.complete: Conversation processing complete
    - memory.processed: Memory extraction complete

    All events are logged to SQLite database with full context for test verification.
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ["transcript", "conversation", "memory"]

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.storage = EventStorage(
            db_path=config.get("db_path", "/app/debug/test_plugin_events.db")
        )
        self.event_count = 0

    async def initialize(self):
        """Initialize the test plugin and event storage"""
        try:
            await self.storage.initialize()
            logger.info("✅ Test Event Plugin initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Test Event Plugin: {e}")
            raise

    async def on_transcript(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Log transcript events (streaming or batch).

        Context data contains:
        - transcript: str - The transcript text
        - conversation_id: str - Conversation ID
        - For streaming: is_final, confidence, words, segments
        - For batch: word_count, segments

        Args:
            context: Plugin context with event data

        Returns:
            PluginResult indicating success
        """
        try:
            # Determine which transcript event this is based on context.event
            event_type = context.event  # 'transcript.streaming' or 'transcript.batch'

            # Extract key data fields
            transcript = context.data.get("transcript", "")
            conversation_id = context.data.get("conversation_id", "unknown")

            # Log to storage
            row_id = await self.storage.log_event(
                event=event_type,
                user_id=context.user_id,
                data=context.data,
                metadata=context.metadata,
            )

            self.event_count += 1

            logger.info(
                f"📝 Logged {event_type} event (row_id={row_id}): "
                f"user={context.user_id}, "
                f"conversation={conversation_id}, "
                f"transcript='{transcript[:50]}...'"
            )

            return PluginResult(
                success=True,
                message=f"Transcript event logged (row_id={row_id})",
                should_continue=True,  # Don't block normal processing
            )

        except Exception as e:
            logger.error(f"Error logging transcript event: {e}", exc_info=True)
            return PluginResult(
                success=False,
                message=f"Failed to log transcript event: {e}",
                should_continue=True,
            )

    async def on_conversation_complete(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Log conversation completion events.

        Context data contains:
        - conversation: dict - Full conversation data
        - transcript: str - Complete conversation transcript
        - duration: float - Conversation duration
        - conversation_id: str - Conversation identifier

        Args:
            context: Plugin context with event data

        Returns:
            PluginResult indicating success
        """
        conversation_id = context.data.get("conversation_id", "unknown")
        duration = context.data.get("duration", 0)

        # Add at start
        logger.info(
            f"📝 HANDLER: on_conversation_complete called for {conversation_id[:12]}"
        )
        logger.debug(f"   Event: {context.event}")
        logger.debug(f"   Metadata: {context.metadata}")
        logger.debug(f"   Duration: {duration}s")

        try:
            # Add before storage
            logger.info(f"   💾 Storing event to SQLite database...")

            row_id = await self.storage.log_event(
                event=context.event,  # 'conversation.complete'
                user_id=context.user_id,
                data=context.data,
                metadata=context.metadata,
            )

            # Add after storage
            logger.info(f"   ✓ Event stored successfully (row_id={row_id})")

            self.event_count += 1

            return PluginResult(
                success=True,
                message=f"Conversation event logged (row_id={row_id})",
                data={"row_id": row_id},
                should_continue=True,
            )

        except Exception as e:
            # Enhance error logging
            logger.error(
                f"   ✗ Storage FAILED for {conversation_id[:12]}: {e}", exc_info=True
            )
            return PluginResult(
                success=False,
                message=f"Failed to log conversation event: {e}",
                should_continue=True,
            )

    async def on_memory_processed(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Log memory processing events.

        Context data contains:
        - memories: list - Extracted memories
        - conversation: dict - Source conversation
        - memory_count: int - Number of memories created
        - conversation_id: str - Conversation identifier

        Metadata contains:
        - processing_time: float - Time spent processing
        - memory_provider: str - Provider name

        Args:
            context: Plugin context with event data

        Returns:
            PluginResult indicating success
        """
        try:
            conversation_id = context.data.get("conversation_id", "unknown")
            memory_count = context.data.get("memory_count", 0)
            memory_provider = context.metadata.get("memory_provider", "unknown")
            processing_time = context.metadata.get("processing_time", 0)

            # Log to storage
            row_id = await self.storage.log_event(
                event=context.event,  # 'memory.processed'
                user_id=context.user_id,
                data=context.data,
                metadata=context.metadata,
            )

            self.event_count += 1

            logger.info(
                f"📝 Logged memory.processed event (row_id={row_id}): "
                f"user={context.user_id}, "
                f"conversation={conversation_id}, "
                f"memory_count={memory_count}, "
                f"provider={memory_provider}, "
                f"processing_time={processing_time:.2f}s"
            )

            return PluginResult(
                success=True,
                message=f"Memory event logged (row_id={row_id})",
                should_continue=True,
            )

        except Exception as e:
            logger.error(f"Error logging memory event: {e}", exc_info=True)
            return PluginResult(
                success=False,
                message=f"Failed to log memory event: {e}",
                should_continue=True,
            )

    async def cleanup(self):
        """Clean up plugin resources"""
        try:
            logger.info(
                f"🧹 Test Event Plugin shutting down. "
                f"Logged {self.event_count} total events"
            )
            await self.storage.cleanup()
        except Exception as e:
            logger.error(f"Error during test plugin cleanup: {e}")
