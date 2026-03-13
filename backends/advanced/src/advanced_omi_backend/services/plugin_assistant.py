"""Plugin lifecycle assistant — LLM agent with tool calling.

Uses the OpenAI SDK function-calling API to let users create, configure,
and manage plugins through natural conversation.  Destructive tools require
frontend confirmation before execution.
"""

import json
import logging
from typing import AsyncGenerator

from advanced_omi_backend.controllers import system_controller
from advanced_omi_backend.llm_client import async_chat_with_tools
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.prompt_registry import get_prompt_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_plugin_status",
            "description": "Get current status and configuration of all plugins, or a specific plugin by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_id": {
                        "type": "string",
                        "description": "Optional plugin ID to filter. Omit to get all plugins.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_plugin_config",
            "description": (
                "Apply configuration changes to a plugin. Updates orchestration "
                "(enabled, events, condition), settings, and/or environment variables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_id": {
                        "type": "string",
                        "description": "The plugin ID to configure.",
                    },
                    "orchestration": {
                        "type": "object",
                        "description": "Orchestration settings: enabled (bool), events (list of event names), condition (object with type and optional wake_words).",
                    },
                    "settings": {
                        "type": "object",
                        "description": "Plugin-specific settings to update.",
                    },
                    "env_vars": {
                        "type": "object",
                        "description": "Environment variable values (secrets) to set in .env.",
                    },
                },
                "required": ["plugin_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_plugin_connection",
            "description": "Test a plugin's connection/configuration without saving. Returns success/failure with a message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_id": {
                        "type": "string",
                        "description": "The plugin ID to test.",
                    }
                },
                "required": ["plugin_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plugin",
            "description": (
                "Create a new plugin. Optionally provide full plugin.py code "
                "(e.g. LLM-generated implementation). If plugin_code is omitted, "
                "creates standard boilerplate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {
                        "type": "string",
                        "description": "Plugin name in snake_case (e.g. slack_notifier).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what the plugin does.",
                    },
                    "events": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Event names the plugin subscribes to.",
                    },
                    "plugin_code": {
                        "type": "string",
                        "description": "Optional full plugin.py source code.",
                    },
                },
                "required": ["plugin_name", "description", "events"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_plugin_code",
            "description": "Write or update an existing plugin's code. Overwrites plugin.py and updates __init__.py.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_id": {
                        "type": "string",
                        "description": "Plugin identifier (directory name).",
                    },
                    "code": {
                        "type": "string",
                        "description": "New plugin.py source code.",
                    },
                    "config_yml": {
                        "type": "string",
                        "description": "Optional new config.yml content (YAML string).",
                    },
                },
                "required": ["plugin_id", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_plugin",
            "description": "Delete a plugin. Must be disabled first. Removes from plugins.yml and optionally removes files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_id": {
                        "type": "string",
                        "description": "Plugin identifier to delete.",
                    },
                    "remove_files": {
                        "type": "boolean",
                        "description": "Also delete the plugin directory on disk. Default false.",
                    },
                },
                "required": ["plugin_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_events",
            "description": "List all available plugin events with descriptions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_events",
            "description": "Fetch recent plugin event log entries from Redis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max events to return (default 20).",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Filter by event type (e.g. conversation.complete).",
                    },
                },
                "required": [],
            },
        },
    },
]

# Tools that require user confirmation before execution (any action, not just reads)
ACTION_TOOLS = {
    "apply_plugin_config",
    "create_plugin",
    "write_plugin_code",
    "delete_plugin",
    "test_plugin_connection",
}

# ---------------------------------------------------------------------------
# Preview generation for action tools
# ---------------------------------------------------------------------------


async def _generate_preview(name: str, args: dict) -> str:
    """Build a human-readable preview of what an action tool will do."""
    if name == "test_plugin_connection":
        plugin_id = args.get("plugin_id", "?")
        return f"**Test connection for `{plugin_id}`:**\n- Will attempt to reach the plugin's external service"

    if name == "apply_plugin_config":
        plugin_id = args.get("plugin_id", "?")
        parts = [f"**Apply config changes to `{plugin_id}`:**"]
        orch = args.get("orchestration")
        if orch:
            if "enabled" in orch:
                parts.append(f"- {'Enable' if orch['enabled'] else 'Disable'} plugin")
            if "events" in orch:
                parts.append(f"- Set events: {orch['events']}")
            if "condition" in orch:
                parts.append(f"- Set condition: {orch['condition']}")
        if args.get("settings"):
            parts.append(f"- Update settings: {list(args['settings'].keys())}")
        if args.get("env_vars"):
            parts.append(f"- Set env vars: {list(args['env_vars'].keys())}")
        return "\n".join(parts)

    if name == "create_plugin":
        plugin_name = args.get("plugin_name", "?")
        desc = args.get("description", "")
        events = args.get("events", [])
        parts = [
            f"**Create plugin `{plugin_name}`:**",
            f"- Description: {desc}",
            f"- Events: {events}",
            f"- Files: plugin.py, __init__.py, config.yml, README.md",
        ]
        code = args.get("plugin_code")
        if code:
            parts.append(f"\n**Generated code:**\n```python\n{code}\n```")
        else:
            parts.append("- Using standard boilerplate template")
        return "\n".join(parts)

    if name == "write_plugin_code":
        plugin_id = args.get("plugin_id", "?")
        code = args.get("code", "")
        parts = [
            f"**Update code for `{plugin_id}`:**",
            f"- Overwrite plugin.py and update __init__.py",
        ]
        if args.get("config_yml"):
            parts.append("- Also update config.yml")
        parts.append(f"\n**New code:**\n```python\n{code}\n```")
        return "\n".join(parts)

    if name == "delete_plugin":
        plugin_id = args.get("plugin_id", "?")
        remove = args.get("remove_files", False)
        parts = [f"**Delete plugin `{plugin_id}`:**", "- Remove entry from plugins.yml"]
        if remove:
            parts.append(f"- Delete `plugins/{plugin_id}/` directory from disk")
        return "\n".join(parts)

    return f"Execute `{name}` with args: {json.dumps(args, indent=2)}"


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


async def _exec_tool(name: str, arguments: dict) -> dict:
    """Execute a tool call and return the result dict."""
    if name == "get_plugin_status":
        metadata = await system_controller.get_plugins_metadata()
        plugin_id = arguments.get("plugin_id")
        if plugin_id:
            plugins = [
                p
                for p in metadata.get("plugins", [])
                if p.get("plugin_id") == plugin_id
            ]
            return {"plugins": plugins, "status": "success"}
        return metadata

    if name == "apply_plugin_config":
        plugin_id = arguments["plugin_id"]
        config = {k: v for k, v in arguments.items() if k != "plugin_id"}
        return await system_controller.update_plugin_config_structured(
            plugin_id, config
        )

    if name == "test_plugin_connection":
        plugin_id = arguments["plugin_id"]
        return await system_controller.test_plugin_connection(plugin_id, {})

    if name == "create_plugin":
        return await system_controller.create_plugin(
            plugin_name=arguments["plugin_name"],
            description=arguments.get("description", ""),
            events=arguments.get("events", []),
            plugin_code=arguments.get("plugin_code"),
        )

    if name == "write_plugin_code":
        return await system_controller.write_plugin_code(
            plugin_id=arguments["plugin_id"],
            code=arguments["code"],
            config_yml=arguments.get("config_yml"),
        )

    if name == "delete_plugin":
        return await system_controller.delete_plugin(
            plugin_id=arguments["plugin_id"],
            remove_files=arguments.get("remove_files", False),
        )

    if name == "get_available_events":
        return {
            "events": {e.value: e.description for e in PluginEvent},
            "status": "success",
        }

    if name == "get_recent_events":
        from advanced_omi_backend.services.plugin_service import get_plugin_router

        router = get_plugin_router()
        if not router:
            return {"events": [], "status": "plugin_router_not_initialized"}
        limit = arguments.get("limit", 20)
        event_type = arguments.get("event_type")
        events = router.get_recent_events(limit=limit, event_type=event_type)
        return {"events": events, "status": "success"}

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------


async def _build_system_prompt() -> str:
    """Build the system prompt by injecting current plugin metadata and events."""
    metadata = await system_controller.get_plugins_metadata()

    # Compact one-line-per-plugin summary
    lines = []
    for plugin in metadata.get("plugins", []):
        pid = plugin.get("plugin_id", "?")
        name = plugin.get("name", pid)
        enabled = plugin.get("enabled", False)
        orch = plugin.get("orchestration", {})
        events = orch.get("events", [])
        status = "enabled" if enabled else "disabled"
        lines.append(f"- `{pid}` ({name}): {status} | events: {events}")

    plugins_text = "\n".join(lines) if lines else "No plugins discovered."
    plugin_count = str(len(metadata.get("plugins", [])))

    # Build events list from enum
    events_lines = []
    for event in PluginEvent:
        events_lines.append(f"- `{event.value}` — {event.description}")
    events_text = "\n".join(events_lines)

    registry = get_prompt_registry()
    return await registry.get_prompt(
        "plugin_assistant.system",
        plugins_metadata=plugins_text,
        available_events=events_text,
        plugin_count=plugin_count,
    )


# ---------------------------------------------------------------------------
# Agent loop — yields SSE-compatible event dicts
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 8


def _has_confirmation(messages: list[dict], tool_call_id: str) -> bool | str:
    """Check if a tool_call_id has been confirmed or rejected in message history.

    Returns True if confirmed, "rejected" if rejected, False if no decision.
    """
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
            try:
                content = json.loads(msg.get("content", "{}"))
                if content.get("confirmed"):
                    return True
                if content.get("rejected"):
                    return "rejected"
            except (json.JSONDecodeError, TypeError):
                pass
    return False


async def generate_response_stream(messages: list[dict]) -> AsyncGenerator[dict, None]:
    """Run the agent loop, yielding SSE event dicts.

    Event types:
        {"type": "tool_call", "name": "...", "status": "running"}
        {"type": "tool_result", "name": "...", "result": ...}
        {"type": "token", "data": "..."}
        {"type": "confirmation_required", "tool_call_id": "...", ...}
        {"type": "complete"}
        {"type": "error", "data": {"error": "..."}}
    """
    system_prompt = await _build_system_prompt()
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    for _ in range(MAX_TOOL_ROUNDS):
        response = await async_chat_with_tools(
            full_messages, tools=TOOLS, operation="plugin_assistant"
        )
        choice = response.choices[0]

        # If the model wants to call tools, execute them and loop
        if choice.finish_reason == "tool_calls" or choice.message.tool_calls:
            assistant_msg = choice.message.model_dump()
            full_messages.append(assistant_msg)

            needs_confirmation = False

            for tool_call in choice.message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                # Check if this is a destructive tool needing confirmation
                if fn_name in ACTION_TOOLS:
                    confirmation = _has_confirmation(full_messages, tool_call.id)

                    if confirmation == "rejected":
                        # User rejected — add tool result and let model respond
                        full_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps(
                                    {"rejected": True, "reason": "User declined"}
                                ),
                            }
                        )
                        continue

                    if not confirmation:
                        # No confirmation yet — send confirmation request and stop
                        preview = await _generate_preview(fn_name, fn_args)
                        yield {
                            "type": "confirmation_required",
                            "tool_call_id": tool_call.id,
                            "tool_name": fn_name,
                            "tool_args": fn_args,
                            "preview": preview,
                            "assistant_message": assistant_msg,
                        }
                        needs_confirmation = True
                        break

                # Execute tool (non-destructive, or confirmed destructive)
                yield {"type": "tool_call", "name": fn_name, "status": "running"}

                try:
                    result = await _exec_tool(fn_name, fn_args)
                except Exception as e:
                    logger.error(f"Tool {fn_name} failed: {e}")
                    result = {"error": str(e)}

                yield {"type": "tool_result", "name": fn_name, "result": result}

                full_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, default=str),
                    }
                )

            if needs_confirmation:
                # Stop the loop — frontend will re-send with confirmation
                break

            continue  # Loop back to let the model respond with the tool results

        # Plain text response — emit as a single token event
        content = choice.message.content or ""
        yield {"type": "token", "data": content}
        break

    yield {"type": "complete"}
