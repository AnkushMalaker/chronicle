"""
LLM-based command parser for Home Assistant integration.

This module provides structured command parsing using LLM to extract
intent, target entities/areas, and parameters from natural language.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ParsedCommand:
    """Structured representation of a parsed Home Assistant command."""

    action: str
    """Action to perform (e.g., turn_on, turn_off, set_brightness, toggle)"""

    target_type: str
    """Type of target (area, entity, all_in_area)"""

    target: str
    """Target identifier (area name or entity name)"""

    entity_type: Optional[str] = None
    """Entity domain filter (e.g., light, switch, fan) - None means all types"""

    parameters: Dict[str, Any] = field(default_factory=dict)
    """Additional parameters (e.g., brightness_pct=50, color='red')"""


# LLM System Prompt for Command Parsing
COMMAND_PARSER_SYSTEM_PROMPT = """You are a smart home command parser for Home Assistant.

Extract structured information from natural language commands.
Return ONLY valid JSON in this exact format (no markdown, no code blocks, no explanation):

{
  "action": "turn_off",
  "target_type": "area",
  "target": "study",
  "entity_type": "light",
  "parameters": {}
}

ACTIONS (choose one):
- turn_on: Turn on entities
- turn_off: Turn off entities
- toggle: Toggle entity state
- set_brightness: Set brightness level
- set_color: Set color

TARGET_TYPE (choose one):
- area: Targeting all entities of a type in an area (e.g., "study lights")
- all_in_area: Targeting ALL entities in an area (e.g., "everything in study")
- entity: Targeting a specific entity by name (e.g., "desk lamp")

ENTITY_TYPE (optional, use null if not specified):
- light: Light entities
- switch: Switch entities
- fan: Fan entities
- cover: Covers/blinds
- null: All entity types (when target_type is "all_in_area")

PARAMETERS (optional, empty dict if none):
- brightness_pct: Brightness percentage (0-100)
- color: Color name (e.g., "red", "blue", "warm white")

EXAMPLES:

Command: "turn off study lights"
Response: {"action": "turn_off", "target_type": "area", "target": "study", "entity_type": "light", "parameters": {}}

Command: "turn off everything in study"
Response: {"action": "turn_off", "target_type": "all_in_area", "target": "study", "entity_type": null, "parameters": {}}

Command: "turn on desk lamp"
Response: {"action": "turn_on", "target_type": "entity", "target": "desk lamp", "entity_type": null, "parameters": {}}

Command: "set study lights to 50%"
Response: {"action": "set_brightness", "target_type": "area", "target": "study", "entity_type": "light", "parameters": {"brightness_pct": 50}}

Command: "turn on living room fan"
Response: {"action": "turn_on", "target_type": "area", "target": "living room", "entity_type": "fan", "parameters": {}}

Command: "turn off all lights"
Response: {"action": "turn_off", "target_type": "entity", "target": "all", "entity_type": "light", "parameters": {}}

Command: "toggle hallway light"
Response: {"action": "toggle", "target_type": "entity", "target": "hallway light", "entity_type": null, "parameters": {}}

Remember:
1. Return ONLY the JSON object, no markdown formatting
2. Use lowercase for action, target_type, target, entity_type
3. Use null (not "null" string) for missing entity_type
4. Always include all 5 fields: action, target_type, target, entity_type, parameters
"""
