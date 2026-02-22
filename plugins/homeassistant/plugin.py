"""
Home Assistant plugin for Chronicle.

Enables control of Home Assistant devices through natural language commands
triggered by a keyword anywhere in the transcript.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

from .entity_cache import EntityCache
from .mcp_client import HAMCPClient, MCPError

logger = logging.getLogger(__name__)


class HomeAssistantPlugin(BasePlugin):
    """
    Plugin for controlling Home Assistant devices via keyword commands.

    Example:
        User says: "Turn off the hall lights, VV"
        -> Keyword "vv" detected anywhere in transcript by router
        -> Command "Turn off the hall lights" passed to on_transcript()
        -> Plugin parses command and calls HA to execute
        -> Returns: PluginResult with "I've turned off the hall light"
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ["transcript", "button"]

    name = "Home Assistant"
    description = "Keyword-triggered device control with Home Assistant integration"

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Home Assistant plugin.

        Args:
            config: Plugin configuration with keys:
                - ha_url: Home Assistant URL
                - ha_token: Long-lived access token
                - wake_word: Wake word for triggering commands (handled by router)
                - enabled: Whether plugin is enabled
                - access_level: Should be 'transcript'
                - trigger: Should be {'type': 'wake_word', 'wake_word': '...'}
        """
        super().__init__(config)
        self.mcp_client: Optional[HAMCPClient] = None
        self.available_tools: List[Dict] = []
        self.entities: Dict[str, Dict] = {}

        # Entity cache for area-based commands
        self.entity_cache: Optional[EntityCache] = None
        self.cache_initialized = False

        # Configuration
        self.ha_url = config.get("ha_url", "http://localhost:8123")
        self.ha_token = config.get("ha_token", "")
        self.wake_word = config.get("wake_word", "vivi")
        self.timeout = int(config.get("timeout", 30))
        self.button_actions = config.get("button_actions", {})

    def register_prompts(self, registry) -> None:
        """Register Home Assistant prompts with the prompt registry."""
        from .command_parser import COMMAND_PARSER_SYSTEM_PROMPT

        registry.register_default(
            "plugin.homeassistant.command_parser",
            template=COMMAND_PARSER_SYSTEM_PROMPT,
            name="Home Assistant Command Parser",
            description="Parses natural language into structured Home Assistant commands.",
            category="plugin",
            plugin_id="homeassistant",
        )

    async def initialize(self):
        """
        Initialize the Home Assistant plugin.

        Connects to Home Assistant MCP server and discovers available tools.

        Raises:
            MCPError: If connection or discovery fails
        """
        if not self.enabled:
            logger.info("Home Assistant plugin is disabled, skipping initialization")
            return

        if not self.ha_token:
            raise ValueError("Home Assistant token is required")

        logger.info(f"Initializing Home Assistant plugin (URL: {self.ha_url})")

        # Create MCP client (used for REST API calls, not MCP protocol)
        self.mcp_client = HAMCPClient(
            base_url=self.ha_url, token=self.ha_token, timeout=self.timeout
        )

        # Test basic API connectivity with Template API
        try:
            logger.info("Testing Home Assistant API connectivity...")
            test_result = await self.mcp_client._render_template("{{ 1 + 1 }}")
            if str(test_result).strip() != "2":
                raise ValueError(f"Unexpected template result: {test_result}")
            logger.info("Home Assistant API connection successful")
        except Exception as e:
            raise MCPError(f"Failed to connect to Home Assistant API: {e}")

        logger.info("Home Assistant plugin initialized successfully")

    async def on_transcript(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Execute Home Assistant command from wake word transcript.

        Called by the router when a wake word is detected in the transcript.
        The router has already stripped the wake word and extracted the command.

        Args:
            context: PluginContext containing:
                - user_id: User ID who issued the command
                - access_level: 'transcript'
                - data: Dict with:
                    - command: str - Command with wake word already stripped
                    - original_transcript: str - Full transcript with wake word
                    - transcript: str - Original transcript
                    - segment_id: str - Unique segment identifier
                    - conversation_id: str - Current conversation ID
                - metadata: Optional additional metadata

        Returns:
            PluginResult with:
                - success: True if command executed
                - message: User-friendly response
                - data: Dict with action details
                - should_continue: False to stop normal processing

        Example:
            Context data:
                {
                    'command': 'turn off study lights',
                    'original_transcript': 'vivi turn off study lights',
                    'conversation_id': 'conv_123'
                }

            Returns:
                PluginResult(
                    success=True,
                    message="I've turned off 1 light in study",
                    data={'action': 'turn_off', 'entity_ids': ['light.tubelight_3']},
                    should_continue=False
                )
        """
        command = context.data.get("command", "")

        if not command:
            return PluginResult(
                success=False, message="No command provided", should_continue=True
            )

        if not self.mcp_client:
            logger.error("MCP client not initialized")
            return PluginResult(
                success=False,
                message="Sorry, Home Assistant is not connected",
                should_continue=True,
            )

        try:
            conversation_id = context.data.get("conversation_id")

            # Step 0: Extract just the HA command from mixed transcript
            extracted = await self._extract_ha_command(
                command, conversation_id=conversation_id
            )
            if extracted:
                logger.info(f"Extracted HA command: '{extracted}' (from: '{command}')")
                command = extracted

            # Step 1: Parse command using hybrid LLM + fallback parsing
            logger.info(f"Processing HA command: '{command}'")
            parsed = await self._parse_command_hybrid(
                command, conversation_id=conversation_id
            )

            if not parsed:
                return PluginResult(
                    success=False,
                    message="Sorry, I couldn't understand that command",
                    should_continue=True,
                )

            # Step 2: Resolve entities from parsed command
            try:
                entity_ids = await self._resolve_entities(parsed)
            except ValueError as e:
                logger.warning(f"Entity resolution failed: {e}")
                return PluginResult(success=False, message=str(e), should_continue=True)

            # Step 3: Determine service and domain
            # Extract domain from first entity (all should have same domain for area-based)
            domain = entity_ids[0].split(".")[0] if entity_ids else "light"

            # Map action to service name
            service_map = {
                "turn_on": "turn_on",
                "turn_off": "turn_off",
                "toggle": "toggle",
                "set_brightness": "turn_on",  # brightness uses turn_on with params
                "set_color": "turn_on",  # color uses turn_on with params
            }
            service = service_map.get(parsed.action, "turn_on")

            # Step 4: Call Home Assistant service
            logger.info(
                f"Calling {domain}.{service} for {len(entity_ids)} entities: {entity_ids}"
            )

            result = await self.mcp_client.call_service(
                domain=domain,
                service=service,
                entity_ids=entity_ids,
                **parsed.parameters,
            )

            # Step 5: Format user-friendly response
            entity_type_name = parsed.entity_type or domain
            if parsed.target_type == "all":
                message = (
                    f"I've {parsed.action.replace('_', ' ')} {len(entity_ids)} "
                    f"{entity_type_name}{'s' if len(entity_ids) != 1 else ''} everywhere"
                )
            elif parsed.target_type == "area":
                message = (
                    f"I've {parsed.action.replace('_', ' ')} {len(entity_ids)} "
                    f"{entity_type_name}{'s' if len(entity_ids) != 1 else ''} "
                    f"in {parsed.target}"
                )
            elif parsed.target_type == "all_in_area":
                message = (
                    f"I've {parsed.action.replace('_', ' ')} {len(entity_ids)} "
                    f"entities in {parsed.target}"
                )
            else:
                message = f"I've {parsed.action.replace('_', ' ')} {parsed.target}"

            logger.info(f"HA command executed successfully: {message}")

            return PluginResult(
                success=True,
                data={
                    "action": parsed.action,
                    "entity_ids": entity_ids,
                    "target_type": parsed.target_type,
                    "target": parsed.target,
                    "ha_result": result,
                },
                message=message,
                should_continue=False,  # Stop normal processing - HA command handled
            )

        except MCPError as e:
            logger.error(f"Home Assistant API error: {e}", exc_info=True)
            return PluginResult(
                success=False,
                message=f"Sorry, Home Assistant couldn't execute that: {e}",
                should_continue=True,
            )
        except Exception as e:
            logger.error(f"Command execution failed: {e}", exc_info=True)
            return PluginResult(
                success=False,
                message="Sorry, something went wrong while executing that command",
                should_continue=True,
            )

    async def on_plugin_action(self, context: PluginContext) -> Optional[PluginResult]:
        """Handle cross-plugin action calls (e.g., toggle lights from button press).

        Supported actions:
            - toggle_lights / call_service: Call a Home Assistant service on resolved entities
              Data keys: target_type, target, entity_type, service (all optional with defaults)
        """
        action = context.data.get("action", "")

        if action not in ("toggle_lights", "call_service"):
            return PluginResult(
                success=False,
                message=f"Unknown action: {action}",
            )

        if not self.mcp_client:
            logger.error("MCP client not initialized for plugin action")
            return PluginResult(
                success=False,
                message="Home Assistant is not connected",
            )

        try:
            from .command_parser import ParsedCommand

            # Build a ParsedCommand from the action data
            target_type = context.data.get("target_type", "area")
            target = context.data.get("target", "")
            entity_type = context.data.get("entity_type", "light")
            service = context.data.get("service", "toggle")

            if not target:
                return PluginResult(success=False, message="No target specified")

            parsed = ParsedCommand(
                action=service,
                target_type=target_type,
                target=target,
                entity_type=entity_type,
                parameters={},
            )

            # Resolve entities using existing cache-based resolution
            entity_ids = await self._resolve_entities(parsed)
            domain = entity_ids[0].split(".")[0] if entity_ids else entity_type

            # Call the service
            logger.info(
                f"Plugin action: {domain}.{service} for {len(entity_ids)} entities: {entity_ids}"
            )
            result = await self.mcp_client.call_service(
                domain=domain, service=service, entity_ids=entity_ids
            )

            message = (
                f"Called {domain}.{service} on {len(entity_ids)} "
                f"{entity_type}{'s' if len(entity_ids) != 1 else ''} in {target}"
            )
            logger.info(f"Plugin action executed: {message}")

            return PluginResult(
                success=True,
                data={
                    "action": service,
                    "entity_ids": entity_ids,
                    "ha_result": result,
                },
                message=message,
                should_continue=True,
            )

        except ValueError as e:
            logger.warning(f"Entity resolution failed for plugin action: {e}")
            return PluginResult(success=False, message=str(e))
        except MCPError as e:
            logger.error(f"HA API error during plugin action: {e}")
            return PluginResult(success=False, message=f"Home Assistant error: {e}")
        except Exception as e:
            logger.error(f"Plugin action failed: {e}", exc_info=True)
            return PluginResult(success=False, message=f"Action failed: {e}")

    async def on_button_event(self, context: PluginContext) -> Optional[PluginResult]:
        """Handle button events using configured button_actions mappings.

        Maps button event types (single_press, double_press) to HA service calls
        using the button_actions config. Reuses the same entity resolution and
        service call logic as on_plugin_action().
        """
        from advanced_omi_backend.plugins.events import PluginEvent

        from .command_parser import ParsedCommand

        # Map event to config key
        if context.event == PluginEvent.BUTTON_DOUBLE_PRESS:
            action_key = "double_press"
        elif context.event == PluginEvent.BUTTON_SINGLE_PRESS:
            action_key = "single_press"
        else:
            return None

        action_config = self.button_actions.get(action_key)
        if not action_config:
            logger.info(f"No button_actions config for '{action_key}', skipping")
            return None

        if not self.mcp_client:
            logger.error("MCP client not initialized for button event")
            return PluginResult(
                success=False,
                message="Home Assistant is not connected",
            )

        try:
            service = action_config.get("service", "toggle")
            target_type = action_config.get("target_type", "area")
            target = action_config.get("target", "")
            entity_type = action_config.get("entity_type", "light")

            if not target:
                return PluginResult(
                    success=False, message="No target in button_actions config"
                )

            parsed = ParsedCommand(
                action=service,
                target_type=target_type,
                target=target,
                entity_type=entity_type,
                parameters={},
            )

            entity_ids = await self._resolve_entities(parsed)
            domain = entity_ids[0].split(".")[0] if entity_ids else entity_type

            logger.info(
                f"Button {action_key}: {domain}.{service} for {len(entity_ids)} entities: {entity_ids}"
            )
            result = await self.mcp_client.call_service(
                domain=domain, service=service, entity_ids=entity_ids
            )

            message = (
                f"Button {action_key}: {domain}.{service} on {len(entity_ids)} "
                f"{entity_type}{'s' if len(entity_ids) != 1 else ''} in {target}"
            )
            logger.info(f"Button action executed: {message}")

            return PluginResult(
                success=True,
                data={
                    "action": service,
                    "entity_ids": entity_ids,
                    "trigger": action_key,
                    "ha_result": result,
                },
                message=message,
                should_continue=True,
            )

        except ValueError as e:
            logger.warning(f"Entity resolution failed for button action: {e}")
            return PluginResult(success=False, message=str(e))
        except MCPError as e:
            logger.error(f"HA API error during button action: {e}")
            return PluginResult(success=False, message=f"Home Assistant error: {e}")
        except Exception as e:
            logger.error(f"Button action failed: {e}", exc_info=True)
            return PluginResult(success=False, message=f"Button action failed: {e}")

    async def health_check(self) -> dict:
        """Ping Home Assistant API using the initialized client."""
        import time

        if not self.mcp_client:
            return {"ok": False, "message": "MCP client not initialized"}

        try:
            start = time.time()
            result = await self.mcp_client._render_template("{{ 1 + 1 }}")
            latency_ms = int((time.time() - start) * 1000)
            if str(result).strip() == "2":
                return {"ok": True, "message": "Connected", "latency_ms": latency_ms}
            return {
                "ok": False,
                "message": f"Unexpected result: {result}",
                "latency_ms": latency_ms,
            }
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def cleanup(self):
        """Clean up resources"""
        if self.mcp_client:
            await self.mcp_client.close()
            logger.info("Closed Home Assistant MCP client")

    async def _ensure_cache_initialized(self):
        """Ensure entity cache is initialized. Lazy-load on first use."""
        if not self.cache_initialized:
            logger.info("Entity cache not initialized, refreshing...")
            await self._refresh_cache()
            self.cache_initialized = True

    async def _refresh_cache(self):
        """
        Refresh the entity cache from Home Assistant.

        Fetches:
        - All areas
        - Entities in each area
        - Entity state details
        """
        if not self.mcp_client:
            logger.error("Cannot refresh cache: MCP client not initialized")
            return

        try:
            logger.info("Refreshing entity cache from Home Assistant...")

            # Fetch all areas
            areas = await self.mcp_client.fetch_areas()
            logger.debug(f"Fetched {len(areas)} areas: {areas}")

            # Fetch entities for each area
            area_entities = {}
            for area in areas:
                entities = await self.mcp_client.fetch_area_entities(area)
                area_entities[area] = entities
                logger.debug(f"Area '{area}': {len(entities)} entities")

            # Fetch labels and build label → areas mapping
            label_areas_map = {}
            try:
                labels = await self.mcp_client.fetch_labels()
                for label in labels:
                    label_area_list = await self.mcp_client.fetch_label_areas(label)
                    if label_area_list:
                        label_areas_map[label] = label_area_list
                if label_areas_map:
                    logger.info(f"Label→area mappings: {label_areas_map}")
            except Exception as e:
                logger.warning(f"Failed to fetch labels (non-fatal): {e}")

            # Fetch all entity states
            entity_details = await self.mcp_client.fetch_entity_states()
            logger.debug(f"Fetched {len(entity_details)} entity states")

            # Create cache
            from datetime import datetime

            self.entity_cache = EntityCache(
                areas=areas,
                area_entities=area_entities,
                label_areas=label_areas_map,
                entity_details=entity_details,
                last_refresh=datetime.now(),
            )

            logger.info(
                f"Entity cache refreshed: {len(areas)} areas, "
                f"{len(entity_details)} entities"
            )

        except Exception as e:
            logger.error(f"Failed to refresh entity cache: {e}", exc_info=True)
            raise

    async def _extract_ha_command(
        self, transcript: str, *, conversation_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Use a lightweight LLM call to extract only the Home Assistant command
        from a transcript that may contain mixed conversation.

        When the keyword (e.g. "vivi") is detected anywhere in a long transcript,
        the surrounding text often includes unrelated speech. This method asks the
        LLM to return just the smart-home command portion.

        Args:
            transcript: Transcript text with keyword already stripped.
            conversation_id: Optional conversation ID for Langfuse session grouping.

        Returns:
            Extracted command string, or None to fall back to the raw text.
        """
        # Short transcripts are likely already just the command
        if len(transcript.split()) <= 8:
            return None

        try:
            from advanced_omi_backend.llm_client import get_llm_client
            from advanced_omi_backend.openai_factory import is_langfuse_enabled

            llm_client = get_llm_client()

            system_prompt = (
                "Extract ONLY the smart home / home assistant command from the "
                "transcript below. The transcript is from a conversation and may "
                "contain unrelated speech mixed in. Return ONLY the command text, "
                "nothing else. If no smart home command is found, return NONE.\n\n"
                "Examples:\n"
                'Input: "so anyway I was saying turn off the hall lights and then we went to dinner"\n'
                "Output: turn off the hall lights\n\n"
                'Input: "turn on bedroom lights"\n'
                "Output: turn on bedroom lights\n\n"
                'Input: "yeah the meeting was great oh and set living room brightness '
                'to 50 percent and also the deadline is tomorrow"\n'
                "Output: set living room brightness to 50 percent\n\n"
                'Input: "so I told him about the project and then toggle the kitchen fan '
                'and after that we discussed lunch plans"\n'
                "Output: toggle the kitchen fan"
            )

            params = {
                "model": llm_client.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": transcript},
                ],
                "temperature": 0.0,
                "max_tokens": 100,
            }
            if is_langfuse_enabled():
                params["name"] = "ha-command-extraction"
                params["metadata"] = {
                    "plugin": "homeassistant",
                    "step": "extract_command",
                }
                if conversation_id:
                    params["langfuse_session_id"] = conversation_id

            response = llm_client.client.chat.completions.create(**params)

            result = response.choices[0].message.content.strip()

            if not result or result.upper() == "NONE":
                logger.info("LLM extraction found no HA command in transcript")
                return None

            logger.info(f"LLM extracted HA command: '{result}'")
            return result

        except Exception as e:
            logger.warning(f"LLM command extraction failed: {e}, using raw text")
            return None

    async def _parse_command_with_llm(
        self, command: str, *, conversation_id: Optional[str] = None
    ) -> Optional["ParsedCommand"]:
        """
        Parse command using LLM with structured system prompt.

        Args:
            command: Natural language command (wake word already stripped)

        Returns:
            ParsedCommand if parsing succeeds, None otherwise

        Example:
            >>> await self._parse_command_with_llm("turn off study lights")
            ParsedCommand(
                action="turn_off",
                target_type="area",
                target="study",
                entity_type="light",
                parameters={}
            )
        """
        try:
            from advanced_omi_backend.llm_client import get_llm_client
            from advanced_omi_backend.openai_factory import is_langfuse_enabled
            from advanced_omi_backend.prompt_registry import get_prompt_registry

            from .command_parser import ParsedCommand

            llm_client = get_llm_client()
            registry = get_prompt_registry()
            system_prompt = await registry.get_prompt(
                "plugin.homeassistant.command_parser"
            )

            logger.debug(f"Parsing command with LLM: '{command}'")

            # Build context from entity cache so the LLM knows valid targets
            entity_context = ""
            await self._ensure_cache_initialized()
            if self.entity_cache:
                areas = (
                    ", ".join(self.entity_cache.areas)
                    if self.entity_cache.areas
                    else "none"
                )
                labels = ""
                if self.entity_cache.label_areas:
                    label_parts = [
                        f"{lbl} (covers: {', '.join(areas_list)})"
                        for lbl, areas_list in self.entity_cache.label_areas.items()
                    ]
                    labels = "\nAvailable labels: " + ", ".join(label_parts)
                entity_context = f"\n\nAvailable areas: {areas}{labels}\nUse target_type 'area' with an area/label name above, or target_type 'all' for everything."

            # Use OpenAI chat format with system + user messages
            params = {
                "model": llm_client.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f'Command: "{command}"{entity_context}\n\nReturn JSON only.',
                    },
                ],
                "temperature": 0.1,
                "max_tokens": 150,
            }
            if is_langfuse_enabled():
                params["name"] = "ha-command-parser"
                params["metadata"] = {
                    "plugin": "homeassistant",
                    "step": "parse_command",
                }
                if conversation_id:
                    params["langfuse_session_id"] = conversation_id

            response = llm_client.client.chat.completions.create(**params)

            result_text = response.choices[0].message.content.strip()
            logger.debug(f"LLM response: {result_text}")

            # Remove markdown code blocks if present
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1]) if len(lines) > 2 else result_text
                result_text = result_text.strip()

            # Parse JSON response
            result_json = json.loads(result_text)

            # Validate required fields
            required_fields = ["action", "target_type", "target"]
            if not all(field in result_json for field in required_fields):
                logger.warning(f"LLM response missing required fields: {result_json}")
                return None

            parsed = ParsedCommand(
                action=result_json["action"],
                target_type=result_json["target_type"],
                target=result_json["target"],
                entity_type=result_json.get("entity_type"),
                parameters=result_json.get("parameters", {}),
            )

            logger.info(
                f"LLM parsed command: action={parsed.action}, "
                f"target_type={parsed.target_type}, target={parsed.target}, "
                f"entity_type={parsed.entity_type}"
            )

            return parsed

        except json.JSONDecodeError as e:
            logger.error(
                f"Failed to parse LLM JSON response: {e}\nResponse: {result_text}"
            )
            return None
        except Exception as e:
            logger.error(f"LLM command parsing failed: {e}", exc_info=True)
            return None

    async def _resolve_entities(self, parsed: "ParsedCommand") -> List[str]:
        """
        Resolve ParsedCommand to actual Home Assistant entity IDs.

        Args:
            parsed: ParsedCommand from LLM parsing

        Returns:
            List of entity IDs to target

        Raises:
            ValueError: If target not found or ambiguous

        Example:
            >>> await self._resolve_entities(ParsedCommand(
            ...     action="turn_off",
            ...     target_type="area",
            ...     target="study",
            ...     entity_type="light"
            ... ))
            ["light.tubelight_3"]
        """
        from .command_parser import ParsedCommand

        # Ensure cache is ready
        await self._ensure_cache_initialized()

        if not self.entity_cache:
            raise ValueError("Entity cache not initialized")

        if parsed.target_type == "area":
            # Get entities in area, filtered by type
            entities = self.entity_cache.get_entities_in_area(
                area=parsed.target, entity_type=parsed.entity_type
            )

            if not entities:
                entity_desc = (
                    f"{parsed.entity_type}s" if parsed.entity_type else "entities"
                )
                available = list(self.entity_cache.areas) + list(
                    self.entity_cache.label_areas.keys()
                )
                raise ValueError(
                    f"No {entity_desc} found in area/label '{parsed.target}'. "
                    f"Available: {', '.join(available)}"
                )

            logger.info(
                f"Resolved area '{parsed.target}' to {len(entities)} "
                f"{parsed.entity_type or 'entity'}(s)"
            )
            return entities

        elif parsed.target_type == "all":
            # Get entities across ALL areas, optionally filtered by type
            entities = []
            for area in self.entity_cache.areas:
                entities.extend(
                    self.entity_cache.get_entities_in_area(
                        area=area, entity_type=parsed.entity_type
                    )
                )

            if not entities:
                entity_desc = (
                    f"{parsed.entity_type}s" if parsed.entity_type else "entities"
                )
                raise ValueError(f"No {entity_desc} found in any area")

            logger.info(
                f"Resolved 'all' to {len(entities)} "
                f"{parsed.entity_type or 'entity'}(s) across {len(self.entity_cache.areas)} areas"
            )
            return entities

        elif parsed.target_type == "all_in_area":
            # Get ALL entities in area (no filter)
            entities = self.entity_cache.get_entities_in_area(
                area=parsed.target, entity_type=None
            )

            if not entities:
                raise ValueError(
                    f"No entities found in area '{parsed.target}'. "
                    f"Available areas: {', '.join(self.entity_cache.areas)}"
                )

            logger.info(
                f"Resolved 'all in {parsed.target}' to {len(entities)} entities"
            )
            return entities

        elif parsed.target_type == "entity":
            # Fuzzy match entity by name
            entity_id = self.entity_cache.find_entity_by_name(parsed.target)

            if not entity_id:
                raise ValueError(
                    f"Entity '{parsed.target}' not found. "
                    f"Try being more specific or check the entity name."
                )

            logger.info(f"Resolved entity '{parsed.target}' to {entity_id}")
            return [entity_id]

        else:
            raise ValueError(f"Unknown target type: {parsed.target_type}")

    async def _parse_command_fallback(self, command: str) -> Optional[Dict[str, Any]]:
        """
        Fallback keyword-based command parser (used when LLM fails).

        Args:
            command: Natural language command

        Returns:
            Dict with 'tool', 'arguments', and optional metadata
            None if parsing fails

        Example:
            Input: "turn off the hall lights"
            Output: {
                "tool": "turn_off",
                "arguments": {"entity_id": "light.hall_light"},
                "friendly_name": "Hall Light",
                "action": "turn_off"
            }
        """
        logger.debug("Using fallback keyword-based parsing")
        command_lower = command.lower().strip()

        # Determine action
        tool = None
        if any(word in command_lower for word in ["turn off", "off", "disable"]):
            tool = "turn_off"
            action_desc = "turned off"
        elif any(word in command_lower for word in ["turn on", "on", "enable"]):
            tool = "turn_on"
            action_desc = "turned on"
        elif "toggle" in command_lower:
            tool = "toggle"
            action_desc = "toggled"
        else:
            logger.warning(f"Unknown action in command: {command}")
            return None

        # Extract entity name from command
        entity_query = command_lower
        for action_word in ["turn off", "turn on", "toggle", "off", "on", "the"]:
            entity_query = entity_query.replace(action_word, "").strip()

        logger.info(f"Searching for entity: '{entity_query}'")

        # Return placeholder (this will work if entity ID matches pattern)
        return {
            "tool": tool,
            "arguments": {"entity_id": f"light.{entity_query.replace(' ', '_')}"},
            "friendly_name": entity_query.title(),
            "action_desc": action_desc,
        }

    async def _parse_command_hybrid(
        self, command: str, *, conversation_id: Optional[str] = None
    ) -> Optional["ParsedCommand"]:
        """
        Hybrid command parser: Try LLM first, fallback to keywords.

        This provides the best of both worlds:
        - LLM parsing for complex area-based and natural commands
        - Keyword fallback for reliability when LLM fails or times out

        Args:
            command: Natural language command
            conversation_id: Optional conversation ID for Langfuse session grouping.

        Returns:
            ParsedCommand if successful, None otherwise

        Example:
            >>> await self._parse_command_hybrid("turn off study lights")
            ParsedCommand(action="turn_off", target_type="area", target="study", ...)
        """
        import asyncio

        from .command_parser import ParsedCommand

        # Try LLM parsing with timeout
        try:
            logger.debug("Attempting LLM-based command parsing...")
            parsed = await asyncio.wait_for(
                self._parse_command_with_llm(command, conversation_id=conversation_id),
                timeout=5.0,
            )

            if parsed:
                logger.info("LLM parsing succeeded")
                return parsed
            else:
                logger.warning("LLM parsing returned None, falling back to keywords")

        except asyncio.TimeoutError:
            logger.warning("LLM parsing timed out (>5s), falling back to keywords")
        except Exception as e:
            logger.warning(f"LLM parsing failed: {e}, falling back to keywords")

        # Fallback to keyword-based parsing
        try:
            logger.debug("Using fallback keyword parsing...")
            fallback_result = await self._parse_command_fallback(command)

            if not fallback_result:
                return None

            # Convert fallback format to ParsedCommand
            # Extract entity_id from arguments
            entity_id = fallback_result["arguments"].get("entity_id", "")
            entity_name = entity_id.split(".", 1)[1] if "." in entity_id else entity_id

            # Simple heuristic: assume it's targeting a single entity
            parsed = ParsedCommand(
                action=fallback_result["tool"],
                target_type="entity",
                target=entity_name.replace("_", " "),
                entity_type=None,
                parameters={},
            )

            logger.info("Fallback parsing succeeded")
            return parsed

        except Exception as e:
            logger.error(f"Fallback parsing failed: {e}", exc_info=True)
            return None

    @staticmethod
    async def test_connection(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Test Home Assistant API connection with provided configuration.

        This static method tests the HA API connection without fully initializing the plugin.
        Used by the form-based configuration UI to validate settings before saving.

        Args:
            config: Configuration dictionary with HA settings:
                - ha_url: Home Assistant URL
                - ha_token: Long-lived access token
                - timeout: Request timeout (optional, default 30)

        Returns:
            Dict with success status, message, and optional details

        Example:
            >>> result = await HomeAssistantPlugin.test_connection({
            ...     'ha_url': 'http://homeassistant.local:8123',
            ...     'ha_token': 'your_long_lived_token'
            ... })
            >>> result['success']
            True
        """
        import time

        try:
            # Validate required config fields
            required_fields = ["ha_url", "ha_token"]
            missing_fields = [
                field for field in required_fields if not config.get(field)
            ]

            if missing_fields:
                return {
                    "success": False,
                    "message": f"Missing required fields: {', '.join(missing_fields)}",
                    "status": "error",
                }

            ha_url = config.get("ha_url")
            ha_token = config.get("ha_token")
            timeout = config.get("timeout", 30)

            # Create temporary MCP client
            mcp_client = HAMCPClient(base_url=ha_url, token=ha_token, timeout=timeout)

            try:
                # Test API connectivity with Template API
                logger.info(f"Testing Home Assistant API connection to {ha_url}...")
                start_time = time.time()

                test_result = await mcp_client._render_template("{{ 1 + 1 }}")
                connection_time_ms = int((time.time() - start_time) * 1000)

                if str(test_result).strip() != "2":
                    return {
                        "success": False,
                        "message": f"Unexpected template result: {test_result}",
                        "status": "error",
                    }

                # Try to fetch entities count for additional info
                try:
                    entities = await mcp_client.discover_entities()
                    entity_count = len(entities)
                except Exception:
                    entity_count = None

                return {
                    "success": True,
                    "message": f"Successfully connected to Home Assistant at {ha_url}",
                    "status": "success",
                    "details": {
                        "ha_url": ha_url,
                        "connection_time_ms": connection_time_ms,
                        "entity_count": entity_count,
                        "api_test": "Template rendering successful",
                    },
                }
            finally:
                await mcp_client.close()

        except Exception as e:
            logger.error(f"Home Assistant connection test failed: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Connection test failed: {str(e)}",
                "status": "error",
            }
