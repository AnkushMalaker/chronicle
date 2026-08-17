"""
Plugin routing system for multi-level plugin architecture.

Routes pipeline events to appropriate plugins based on access level and triggers.
"""

import asyncio
import json
import logging
import re
import string
import time
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional

from advanced_omi_backend.redis_factory import create_sync_redis
from advanced_omi_backend.services.interaction_modes.registry import InteractionRegistry
from advanced_omi_backend.services.observability import record_event_sync
from advanced_omi_backend.services.sse_publisher import publish_sse_event

from .base import BasePlugin, PluginContext, PluginResult
from .events import PluginEvent

logger = logging.getLogger(__name__)


def normalize_text_for_wake_word(text: str) -> str:
    """
    Normalize text for wake word matching.
    - Lowercase
    - Replace punctuation with spaces
    - Collapse multiple spaces to single space
    - Strip leading/trailing whitespace

    Example:
        "Hey, Hermes!" -> "hey hermes"
        "HEY  HERMES" -> "hey hermes"
        "Hey-Hermes" -> "hey hermes"
    """
    # Lowercase
    text = text.lower()
    # Replace punctuation with spaces (instead of removing, to preserve word boundaries)
    text = text.translate(
        str.maketrans(string.punctuation, " " * len(string.punctuation))
    )
    # Normalize whitespace (collapse multiple spaces to single space)
    text = re.sub(r"\s+", " ", text)
    # Strip leading/trailing whitespace
    return text.strip()


def extract_command_around_keyword(transcript: str, keyword: str) -> str:
    """
    Extract command by removing a keyword from anywhere in the transcript.

    Handles punctuation and spacing around the keyword gracefully.

    Example:
        transcript: "Turn off the lights, Hermes"
        keyword: "hermes"
        -> "Turn off the lights"

        transcript: "Hermes, turn off the lights in the hall"
        keyword: "hermes"
        -> "turn off the lights in the hall"

        transcript: "Turn off the hall lights, Hermes, please"
        keyword: "hermes"
        -> "Turn off the hall lights, please"

    Args:
        transcript: Original transcript text
        keyword: Keyword to remove (will be normalized)

    Returns:
        Command text with keyword removed
    """
    keyword_parts = normalize_text_for_wake_word(keyword).split()
    if not keyword_parts:
        return transcript.strip()

    pattern_parts = [re.escape(part) for part in keyword_parts]
    # Match keyword with optional surrounding punctuation/whitespace
    kw_pattern = r"[\s,.\-!?;:]*".join(pattern_parts)
    # Consume adjacent punctuation/whitespace on both sides
    full_pattern = r"[\s,.\-!?;:]*" + kw_pattern + r"[\s,.\-!?;:]*"

    command = re.sub(
        full_pattern, " ", transcript, count=1, flags=re.IGNORECASE
    ).strip()
    # Collapse any doubled spaces left behind
    command = re.sub(r"\s{2,}", " ", command)
    return command


def extract_command_after_wake_word(transcript: str, wake_word: str) -> str:
    """
    Intelligently extract command after wake word in original transcript.

    Handles punctuation and spacing variations by creating a flexible regex pattern.

    Example:
        transcript: "Hey, Hermes, turn off lights"
        wake_word: "hey hermes"
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
    # Example: "hey" + "hermes" -> r"hey[\s,.\-!?]*hermes[\s,.\-!?]*"
    # The pattern matches the wake word parts with optional punctuation/whitespace between and after
    pattern_parts = [re.escape(part) for part in wake_word_parts]
    # Allow optional punctuation/whitespace between parts
    pattern = r"[\s,.\-!?;:]*".join(pattern_parts)
    # Add trailing punctuation/whitespace consumption after last wake word part
    pattern = "^" + pattern + r"[\s,.\-!?;:]*"

    # Try to match wake word at start of transcript (case-insensitive)
    match = re.match(pattern, transcript, re.IGNORECASE)

    if match:
        # Extract everything after the matched wake word (including trailing punctuation)
        command = transcript[match.end() :].strip()
        return command
    else:
        # Fallback: couldn't find wake word boundary, return full transcript
        logger.warning(
            f"Could not find wake word boundary for '{wake_word}' in '{transcript}', using full transcript"
        )
        return transcript.strip()


class ConditionResult(NamedTuple):
    """Result of a plugin condition check."""

    execute: bool
    extra: Dict[str, Any] = {}


class PluginHealth:
    """Health status for a single plugin."""

    # Possible status values
    REGISTERED = "registered"  # Registered but not yet initialized
    INITIALIZED = "initialized"  # Successfully initialized
    DEGRADED = "degraded"  # External dependency unreachable; retried in background
    FAILED = "failed"  # initialize() raised an exception

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.status: str = self.REGISTERED
        self.error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "plugin_id": self.plugin_id,
            "status": self.status,
        }
        if self.error:
            result["error"] = self.error
        return result


class PluginRouter:
    """Routes pipeline events to appropriate plugins based on event subscriptions"""

    _EVENT_LOG_KEY = "system:event_log"
    _EVENT_LOG_MAX = 1000

    def __init__(self):
        self.plugins: Dict[str, BasePlugin] = {}
        self.plugin_health: Dict[str, PluginHealth] = {}
        self.interaction_registry = InteractionRegistry()
        # Index plugins by event for fast lookup
        self._plugins_by_event: Dict[str, List[str]] = {}
        self._services = None

        # Sync Redis for event logging (works from both FastAPI and RQ workers)
        try:
            self._event_redis = create_sync_redis(decode_responses=True)
        except Exception:
            logger.warning("Could not connect to Redis for event logging")
            self._event_redis = None

    def set_services(self, services) -> None:
        """Attach PluginServices instance for injection into plugin contexts."""
        self._services = services

    def register_plugin(self, plugin_id: str, plugin: BasePlugin):
        """Register a plugin with the router"""
        configured_modes = plugin.config.get("modes") or []
        if not isinstance(configured_modes, list) or not all(
            isinstance(value, str) and value.strip() for value in configured_modes
        ):
            raise ValueError(
                f"plugin '{plugin_id}' modes must be a list of non-empty mode IDs"
            )
        configured_mode_ids = set(configured_modes)
        declared_mode_ids = {
            definition.mode_id for definition in plugin.INTERACTION_MODES
        }
        unknown_mode_ids = configured_mode_ids - declared_mode_ids
        if unknown_mode_ids:
            unknown = ", ".join(sorted(unknown_mode_ids))
            raise ValueError(
                f"plugin '{plugin_id}' config enables undeclared interaction mode(s): {unknown}"
            )
        enabled_modes = tuple(
            definition
            for definition in plugin.INTERACTION_MODES
            if definition.mode_id in configured_mode_ids
        )
        for definition in enabled_modes:
            self.interaction_registry.register(plugin_id, definition)

        self.plugins[plugin_id] = plugin
        self.plugin_health[plugin_id] = PluginHealth(plugin_id)

        # Index by each event
        for event in plugin.events:
            if event not in self._plugins_by_event:
                self._plugins_by_event[event] = []
            self._plugins_by_event[event].append(plugin_id)

        mode_ids = [definition.mode_id for definition in enabled_modes]
        logger.info(
            f"Registered plugin '{plugin_id}' for events: {plugin.events}; "
            f"interaction modes: {mode_ids}"
        )

    def mark_plugin_initialized(self, plugin_id: str) -> None:
        """Mark a plugin as successfully initialized."""
        if plugin_id in self.plugin_health:
            health = self.plugin_health[plugin_id]
            health.status = PluginHealth.INITIALIZED
            health.error = None

    def mark_plugin_failed(self, plugin_id: str, error: str) -> None:
        """Mark a plugin as failed during initialization."""
        if plugin_id in self.plugin_health:
            health = self.plugin_health[plugin_id]
            health.status = PluginHealth.FAILED
            health.error = error
        record_event_sync(
            severity="error",
            category="plugin",
            source=plugin_id,
            title=f"Plugin '{plugin_id}' failed to initialize",
            detail=error,
            metadata={"plugin_id": plugin_id},
        )

    def mark_plugin_degraded(self, plugin_id: str, error: str) -> None:
        """Mark a plugin as degraded: its external dependency is unreachable.

        Unlike FAILED, degraded plugins are retried in the background (see
        plugin_service.run_plugin_recovery). The system event is recorded only on
        the transition into DEGRADED, so retry ticks don't spam the event log.
        """
        health = self.plugin_health.get(plugin_id)
        if health is None:
            return
        transition = health.status != PluginHealth.DEGRADED
        health.status = PluginHealth.DEGRADED
        health.error = error
        if transition:
            record_event_sync(
                severity="warning",
                category="plugin",
                source=plugin_id,
                title=f"Plugin '{plugin_id}' degraded: dependency unreachable",
                detail=error,
                metadata={"plugin_id": plugin_id},
                incident_key=f"plugin-dependency:{plugin_id}",
            )

    def get_health_summary(self) -> Dict[str, Any]:
        """Get health summary for all registered plugins."""
        plugins = [h.to_dict() for h in self.plugin_health.values()]
        statuses = [h.status for h in self.plugin_health.values()]
        return {
            "total": len(plugins),
            "initialized": statuses.count(PluginHealth.INITIALIZED),
            "degraded": statuses.count(PluginHealth.DEGRADED),
            "failed": statuses.count(PluginHealth.FAILED),
            "registered": statuses.count(PluginHealth.REGISTERED),
            "plugins": plugins,
        }

    async def dispatch_event(
        self,
        event: str,
        user_id: str,
        data: Dict,
        metadata: Optional[Dict] = None,
        on_plugin_done: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> List[PluginResult]:
        """
        Dispatch event to all subscribed plugins.

        Args:
            event: Event name (e.g., 'transcript.streaming', 'conversation.complete')
            user_id: User ID for context
            data: Event-specific data
            metadata: Optional metadata
            on_plugin_done: Optional async observer called after each plugin in the
                chain runs, as ``await on_plugin_done(plugin_id, duration_ms,
                result, is_last)``. ``result`` is the PluginResult or ``None`` (a
                decline). Lets callers trace per-plugin latency and react to a
                handoff (e.g. the voice path plays a "thinking" tone when the fast
                handler declines and a slower one takes over). Best-effort: an
                observer exception is logged and never breaks dispatch.

        Returns:
            List of plugin results
        """
        # Add at start
        logger.info(f"🔌 ROUTER: Dispatching '{event}' event (user={user_id})")

        results = []
        executed = []  # Track per-plugin outcomes for event log

        # Get plugins subscribed to this event, ordered by priority (lower first)
        # so they form a deterministic chain of responsibility.
        plugin_ids = sorted(
            self._plugins_by_event.get(event, []),
            key=lambda pid: getattr(self.plugins[pid], "priority", 100),
        )

        if not plugin_ids:
            logger.info(f"🔌 ROUTER: No plugins subscribed to event '{event}'")
        else:
            logger.info(
                f"🔌 ROUTER: Found {len(plugin_ids)} subscribed plugin(s): {plugin_ids}"
            )

        last_index = len(plugin_ids) - 1
        for index, plugin_id in enumerate(plugin_ids):
            plugin = self.plugins[plugin_id]

            if not plugin.enabled:
                logger.info(f"   ⊘ Skipping '{plugin_id}': disabled")
                continue

            # Check execution condition (wake_word, etc.)
            logger.info(f"   → Checking execution condition for '{plugin_id}'")
            condition = await self._should_execute(plugin, data, event=event)
            if not condition.execute:
                logger.info(f"   ⊘ Skipping '{plugin_id}': condition not met")
                continue

            # Execute plugin
            try:
                logger.info(f"   ▶ Executing '{plugin_id}' for event '{event}'")
                # Per-plugin data copy: merge extra context (e.g. wake word
                # command) without mutating the shared data dict.
                plugin_data = {**data, **condition.extra} if condition.extra else data
                context = PluginContext(
                    user_id=user_id,
                    event=event,
                    data=plugin_data,
                    metadata=metadata or {},
                    services=self._services,
                )

                started = time.perf_counter()
                result = await self._execute_plugin(plugin, event, context)
                duration_ms = (time.perf_counter() - started) * 1000.0

                # Notify the observer for every plugin that actually ran — including
                # declines (result is None), so callers can time a "miss" and react
                # to the handoff to the next handler in the chain.
                if on_plugin_done is not None:
                    try:
                        await on_plugin_done(
                            plugin_id,
                            duration_ms,
                            result,
                            is_last=(index == last_index),
                        )
                    except (
                        Exception
                    ):  # noqa: BLE001 - observer must never break dispatch
                        logger.debug(
                            f"on_plugin_done observer failed for '{plugin_id}'",
                            exc_info=True,
                        )

                if result:
                    status_icon = "✓" if result.success else "✗"
                    logger.info(
                        f"   {status_icon} Plugin '{plugin_id}' completed: "
                        f"success={result.success}, message={result.message}"
                    )
                    results.append(result)
                    executed.append(
                        {
                            "plugin_id": plugin_id,
                            "success": result.success,
                            "message": result.message,
                            # Carry the plugin's structured output (reply, command,
                            # skip reason, etc.) into the event log so the Events
                            # page can show *why* a plugin succeeded/failed.
                            "data": result.data,
                        }
                    )

                    # If plugin says stop processing, break
                    if not result.should_continue:
                        logger.info(
                            f"   ⊗ Plugin '{plugin_id}' stopped further processing"
                        )
                        break
                else:
                    logger.info(
                        f"   ⊘ Plugin '{plugin_id}' returned no result for '{event}'"
                    )

            except Exception as e:
                # CRITICAL: Log exception details
                logger.error(
                    f"   ✗ Plugin '{plugin_id}' FAILED with exception: {e}",
                    exc_info=True,
                )
                executed.append(
                    {"plugin_id": plugin_id, "success": False, "message": str(e)}
                )

        # Add at end
        logger.info(
            f"🔌 ROUTER: Dispatch complete for '{event}': "
            f"{len(results)} plugin(s) executed successfully"
        )

        self._log_event(
            event=event,
            user_id=user_id,
            plugins_subscribed=plugin_ids,
            plugins_executed=executed,
            metadata=metadata,
        )

        return results

    _SKIP = ConditionResult(execute=False)
    _PASS = ConditionResult(execute=True)

    async def _should_execute(
        self, plugin: BasePlugin, data: Dict, event: Optional[str] = None
    ) -> ConditionResult:
        """Check if plugin should be executed based on condition configuration.

        Returns a ConditionResult. The ``extra`` dict contains per-plugin data
        (e.g. wake word command extraction) that gets merged into a copy of data
        for the plugin's PluginContext — never mutating the shared data dict.

        Button events bypass transcript-based conditions (wake_word) since they
        have no transcript to match against.
        """
        condition_type = plugin.condition.get("type", "always")

        if condition_type == "always":
            return self._PASS

        # Acoustic wake word: the standalone wakeword-service is the ONLY source of
        # WAKE_WORD_DETECTED. Fire ONLY on that event, gated by the configured score
        # threshold, and never on transcript/button events — so a plugin can be
        # gated EXCLUSIVELY on the acoustic wake word. The command text arrives
        # pre-resolved in data["command"] (set by the wake-word dispatcher).
        if condition_type == "acoustic_wake_word":
            if event == PluginEvent.WAKE_WORD_DETECTED:
                threshold = float(plugin.condition.get("threshold", 0.0) or 0.0)
                raw_score = data.get("score")
                score = float(raw_score) if raw_score is not None else 1.0
                if score >= threshold:
                    return self._PASS
                logger.debug(
                    f"Acoustic wake below threshold ({score:.3f} < {threshold:.3f}); skipping"
                )
            return self._SKIP

        # Button, starred, and acoustic wake-word events bypass transcript-based
        # conditions (wake_word/keyword_anywhere) — they have no transcript to
        # match against. The acoustic detector already arms on the wake word, and
        # the command text arrives pre-resolved in data["command"].
        if event and event in (
            PluginEvent.BUTTON_SINGLE_PRESS,
            PluginEvent.BUTTON_DOUBLE_PRESS,
            PluginEvent.CONVERSATION_STARRED,
            PluginEvent.WAKE_WORD_DETECTED,
        ):
            return self._PASS

        elif condition_type == "wake_word":
            # Normalize transcript for matching (handles punctuation and spacing)
            transcript = data.get("transcript", "")
            normalized_transcript = normalize_text_for_wake_word(transcript)

            # Support both singular 'wake_word' and plural 'wake_words' (list)
            wake_words = plugin.condition.get("wake_words", [])
            if not wake_words:
                # Fallback to singular wake_word for backward compatibility
                wake_word = plugin.condition.get("wake_word", "")
                if wake_word:
                    wake_words = [wake_word]

            # Check if transcript starts with any wake word (after normalization)
            for wake_word in wake_words:
                normalized_wake_word = normalize_text_for_wake_word(wake_word)
                if normalized_wake_word and normalized_transcript.startswith(
                    normalized_wake_word
                ):
                    # Smart extraction: find where wake word actually ends in original text
                    command = extract_command_after_wake_word(transcript, wake_word)
                    logger.debug(
                        f"Wake word '{wake_word}' detected. Original: '{transcript}', Command: '{command}'"
                    )
                    return ConditionResult(
                        execute=True,
                        extra={"command": command, "original_transcript": transcript},
                    )

            return self._SKIP

        elif condition_type == "keyword_anywhere":
            # Trigger when keyword appears anywhere in the transcript.
            # Command is the transcript with the keyword removed.
            transcript = data.get("transcript", "")
            normalized_transcript = normalize_text_for_wake_word(transcript)

            keywords = plugin.condition.get("keywords", [])
            if not keywords:
                keyword = plugin.condition.get("keyword", "")
                if keyword:
                    keywords = [keyword]

            for keyword in keywords:
                normalized_keyword = normalize_text_for_wake_word(keyword)
                if normalized_keyword and normalized_keyword in normalized_transcript:
                    command = extract_command_around_keyword(transcript, keyword)
                    logger.debug(
                        f"Keyword '{keyword}' found in transcript. "
                        f"Original: '{transcript}', Command: '{command}'"
                    )
                    return ConditionResult(
                        execute=True,
                        extra={"command": command, "original_transcript": transcript},
                    )

            return self._SKIP

        elif condition_type == "conditional":
            # Future: Custom condition checking
            return self._PASS

        return self._SKIP

    async def _execute_plugin(
        self, plugin: BasePlugin, event: str, context: PluginContext
    ) -> Optional[PluginResult]:
        """Execute plugin method for specified event"""
        # Map events to plugin callback methods using enums
        # str(Enum) comparisons work because PluginEvent inherits from str
        if event in (PluginEvent.TRANSCRIPT_STREAMING, PluginEvent.TRANSCRIPT_BATCH):
            return await plugin.on_transcript(context)
        elif event in (PluginEvent.CONVERSATION_COMPLETE,):
            return await plugin.on_conversation_complete(context)
        elif event in (PluginEvent.MEMORY_PROCESSED,):
            return await plugin.on_memory_processed(context)
        elif event == PluginEvent.CONVERSATION_STARRED:
            return await plugin.on_conversation_starred(context)
        elif event == PluginEvent.WAKE_WORD_DETECTED:
            return await plugin.on_wake_word_detected(context)
        elif event in (
            PluginEvent.BUTTON_SINGLE_PRESS,
            PluginEvent.BUTTON_DOUBLE_PRESS,
        ):
            return await plugin.on_button_event(context)
        elif event == PluginEvent.PLUGIN_ACTION:
            return await plugin.on_plugin_action(context)

        # Fallback for any unrecognized events (forward compatibility)
        logger.warning(f"No handler mapping for event '{event}'")
        return None

    def _log_event(
        self,
        event: str,
        user_id: str,
        plugins_subscribed: List[str],
        plugins_executed: List[Dict],
        metadata: Optional[Dict] = None,
    ) -> None:
        """Append an event record to the Redis event log (capped list)."""
        if not self._event_redis:
            return
        try:
            record = json.dumps(
                {
                    "timestamp": time.time(),
                    "event": event,
                    "user_id": user_id,
                    "plugins_subscribed": plugins_subscribed,
                    "plugins_executed": plugins_executed,
                    "metadata": metadata or {},
                }
            )
            pipe = self._event_redis.pipeline()
            pipe.lpush(self._EVENT_LOG_KEY, record)
            pipe.ltrim(self._EVENT_LOG_KEY, 0, self._EVENT_LOG_MAX - 1)
            pipe.execute()

            # Publish SSE event for queue page live updates
            if user_id:
                publish_sse_event(
                    user_id,
                    "plugin.event",
                    {
                        "event": event,
                        "plugins_executed": len(plugins_executed),
                    },
                )
        except Exception:
            logger.debug("Failed to log event to Redis", exc_info=True)

    def clear_events(self) -> int:
        """Delete all events from the Redis event log. Returns the number of events that were stored."""
        if not self._event_redis:
            return 0
        try:
            count = self._event_redis.llen(self._EVENT_LOG_KEY)
            self._event_redis.delete(self._EVENT_LOG_KEY)
            return count
        except Exception:
            logger.debug("Failed to clear events from Redis", exc_info=True)
            return 0

    def get_recent_events(
        self, limit: int = 50, event_type: Optional[str] = None
    ) -> List[Dict]:
        """Read recent events from the Redis log."""
        if not self._event_redis:
            return []
        try:
            # Fetch more than needed when filtering by type
            fetch_count = self._EVENT_LOG_MAX if event_type else limit
            raw = self._event_redis.lrange(self._EVENT_LOG_KEY, 0, fetch_count - 1)
            events = [json.loads(r) for r in raw]
            if event_type:
                events = [e for e in events if e.get("event") == event_type][:limit]
            return events
        except Exception:
            logger.debug("Failed to read events from Redis", exc_info=True)
            return []

    async def check_connectivity(self) -> Dict[str, Dict[str, Any]]:
        """Run health_check() on all initialized plugins with a 10s timeout each.

        Returns:
            Dict mapping plugin_id to health check result dict.
        """
        results: Dict[str, Dict[str, Any]] = {}

        for plugin_id, plugin in self.plugins.items():
            health = self.plugin_health.get(plugin_id)
            if not health or health.status != PluginHealth.INITIALIZED:
                results[plugin_id] = {"ok": False, "message": "Not initialized"}
                continue

            try:
                result = await asyncio.wait_for(plugin.health_check(), timeout=10.0)
                results[plugin_id] = result
            except asyncio.TimeoutError:
                results[plugin_id] = {
                    "ok": False,
                    "message": "Health check timed out (10s)",
                }
            except Exception as e:
                results[plugin_id] = {
                    "ok": False,
                    "message": f"Health check error: {e}",
                }

        return results

    def get_asr_keywords(self) -> list[str]:
        """Collect all wake words and keywords from enabled plugins.

        These are meant to be injected into STT providers as keyword
        boosting hints (e.g. Deepgram ``keyterm``, VibeVoice ``context_info``)
        so that the transcription engine is more likely to correctly
        recognise them.

        Returns:
            Deduplicated list of keyword strings.
        """
        seen: set[str] = set()
        result: list[str] = []

        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            condition = plugin.condition or {}
            condition_type = condition.get("type", "always")

            words: list[str] = []
            if condition_type == "wake_word":
                words = condition.get("wake_words", [])
                if not words:
                    w = condition.get("wake_word", "")
                    if w:
                        words = [w]
            elif condition_type == "keyword_anywhere":
                words = condition.get("keywords", [])
                if not words:
                    w = condition.get("keyword", "")
                    if w:
                        words = [w]

            for w in words:
                normalised = w.strip().lower()
                if normalised and normalised not in seen:
                    seen.add(normalised)
                    result.append(normalised)

        for _owner, definition in self.interaction_registry.modes.values():
            for phrase in definition.activation_phrases:
                normalised = phrase.strip().lower()
                if normalised and normalised not in seen:
                    seen.add(normalised)
                    result.append(normalised)

        return result

    async def cleanup_all(self):
        """Clean up all registered plugins"""
        for plugin_id, plugin in self.plugins.items():
            try:
                await plugin.cleanup()
                logger.info(f"Cleaned up plugin '{plugin_id}'")
            except Exception as e:
                logger.error(f"Error cleaning up plugin '{plugin_id}': {e}")
