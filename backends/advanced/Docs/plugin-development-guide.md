# Chronicle Plugin Development Guide

A comprehensive guide to creating custom plugins for Chronicle.

## Table of Contents

1. [Introduction](#introduction)
2. [Quick Start](#quick-start)
3. [Plugin Architecture](#plugin-architecture)
4. [Event Types](#event-types)
5. [Creating Your First Plugin](#creating-your-first-plugin)
6. [Configuration](#configuration)
7. [Testing Plugins](#testing-plugins)
8. [Best Practices](#best-practices)
9. [Examples](#examples)
10. [Troubleshooting](#troubleshooting)

## Introduction

Chronicle's plugin system allows you to extend functionality by subscribing to events and executing custom logic. Plugins are:

- **Event-driven**: React to transcripts, conversations, or memory processing
- **Auto-discovered**: Drop plugins into the `plugins/` directory
- **Configurable**: YAML-based configuration with environment variable support
- **Isolated**: Each plugin runs independently with proper error handling

## Quick Start

### 1. Generate Plugin Boilerplate

```bash
cd backends/advanced
uv run python scripts/create_plugin.py my_awesome_plugin
```

This creates:
```
plugins/my_awesome_plugin/
├── __init__.py           # Plugin exports
├── plugin.py             # Main plugin logic
└── README.md             # Plugin documentation
```

### 2. Implement Plugin Logic

Edit `plugins/my_awesome_plugin/plugin.py`:

```python
async def on_conversation_complete(self, context: PluginContext) -> Optional[PluginResult]:
    """Handle conversation completion."""
    transcript = context.data.get('transcript', '')

    # Your custom logic here
    print(f"Processing: {transcript}")

    return PluginResult(success=True, message="Processing complete")
```

### 3. Configure Plugin

Add to `config/plugins.yml`:

```yaml
plugins:
  my_awesome_plugin:
    enabled: true
    events:
      - conversation.complete
    condition:
      type: always
```

### 4. Restart Backend

```bash
cd backends/advanced
docker compose restart
```

Your plugin will be auto-discovered and loaded!

## Plugin Architecture

### Base Plugin Class

All plugins inherit from `BasePlugin`:

```python
from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

class MyPlugin(BasePlugin):
    SUPPORTED_ACCESS_LEVELS = ['conversation']  # Which events you support

    async def initialize(self):
        """Initialize resources (called on app startup)"""
        pass

    async def cleanup(self):
        """Clean up resources (called on app shutdown)"""
        pass

    async def on_conversation_complete(self, context: PluginContext):
        """Handle conversation.complete events"""
        pass
```

### Plugin Context

Context passed to plugin methods:

```python
@dataclass
class PluginContext:
    user_id: str                    # User identifier
    event: str                      # Event name (e.g., "conversation.complete")
    data: Dict[str, Any]            # Event-specific data
    metadata: Dict[str, Any]        # Additional metadata
```

### Plugin Result

Return value from plugin methods:

```python
@dataclass
class PluginResult:
    success: bool                   # Whether operation succeeded
    data: Optional[Dict[str, Any]]  # Optional result data
    message: Optional[str]          # Optional status message
    should_continue: bool           # Whether to continue normal processing (default: True)
```

## Event Types

### 1. Transcript Events (`transcript.streaming`)

**When**: Real-time transcript segments arrive from WebSocket
**Context Data**:
- `transcript` (str): The transcript text
- `segment_id` (str): Unique segment identifier
- `conversation_id` (str): Current conversation ID

**Use Cases**:
- Wake word detection
- Real-time command processing
- Live transcript analysis

**Example**:
```python
async def on_transcript(self, context: PluginContext):
    transcript = context.data.get('transcript', '')
    if 'urgent' in transcript.lower():
        await self.send_notification(transcript)
```

### 2. Conversation Events (`conversation.complete`)

**When**: Conversation processing finishes
**Context Data**:
- `conversation` (dict): Full conversation data
- `transcript` (str): Complete transcript
- `duration` (float): Conversation duration in seconds
- `conversation_id` (str): Conversation identifier

**Use Cases**:
- Email summaries
- Analytics tracking
- External integrations
- Conversation archiving

**Example**:
```python
async def on_conversation_complete(self, context: PluginContext):
    conversation = context.data.get('conversation', {})
    duration = context.data.get('duration', 0)

    if duration > 300:  # 5 minutes
        await self.archive_long_conversation(conversation)
```

### 3. Memory Events (`memory.processed`)

**When**: Memory extraction finishes
**Context Data**:
- `memories` (list): Extracted memories
- `conversation` (dict): Source conversation
- `memory_count` (int): Number of memories created
- `conversation_id` (str): Conversation identifier

**Use Cases**:
- Memory indexing
- Knowledge graph updates
- Memory notifications
- Analytics

**Example**:
```python
async def on_memory_processed(self, context: PluginContext):
    memories = context.data.get('memories', [])

    for memory in memories:
        await self.index_memory(memory)
```

### 4. Button Events (`button.single_press`, `button.double_press`)

**When**: OMI device button is pressed
**Context Data**:
- `state` (str): Button state (`SINGLE_TAP`, `DOUBLE_TAP`)
- `timestamp` (float): Unix timestamp of the event
- `audio_uuid` (str): Current audio session UUID (may be None)
- `session_id` (str): Streaming session ID (for conversation close)
- `client_id` (str): Client device identifier

**Data Flow**:
```
OMI Device (BLE)
  → Button press on physical device
  → BLE characteristic notifies with 8-byte payload
  ↓
friend-lite-sdk (extras/friend-lite-sdk/)
  → parse_button_event() converts payload → ButtonState IntEnum
  ↓
BLE Client (extras/local-wearable-client/ or mobile app)
  → Formats as Wyoming protocol: {"type": "button-event", "data": {"state": "SINGLE_TAP"}}
  → Sends over WebSocket
  ↓
Backend (websocket_controller.py)
  → _handle_button_event() stores marker on client_state
  → Maps ButtonState → PluginEvent using enums (plugins/events.py)
  → Dispatches granular event to plugin system
  ↓
Plugin System
  → Routed to subscribed plugins (e.g., test_button_actions)
  → Plugins use PluginServices for system actions and cross-plugin calls
```

**Use Cases**:
- Close current conversation (single press)
- Toggle smart home devices (double press)
- Custom actions via cross-plugin communication

**Example**:
```python
async def on_button_event(self, context: PluginContext):
    if context.event == PluginEvent.BUTTON_SINGLE_PRESS:
        session_id = context.data.get('session_id')
        await context.services.close_conversation(session_id)
```

### 5. Plugin Action Events (`plugin_action`)

**When**: Another plugin calls `context.services.call_plugin()`
**Context Data**:
- `action` (str): Action name (e.g., `toggle_lights`)
- Plus any additional data from the calling plugin

**Use Cases**:
- Cross-plugin communication (button press → toggle lights)
- Service orchestration between plugins

**Example**:
```python
async def on_plugin_action(self, context: PluginContext):
    action = context.data.get('action')
    if action == 'toggle_lights':
        # Handle the action
        ...
```

### PluginServices

Plugins receive a `services` object on the context for system and cross-plugin interaction:

```python
# Close the current conversation (triggers post-processing)
await context.services.close_conversation(session_id, reason)

# Call another plugin's on_plugin_action() handler
result = await context.services.call_plugin("homeassistant", "toggle_lights", data)
```

## Creating Your First Plugin

### Step 1: Generate Boilerplate

```bash
uv run python scripts/create_plugin.py todo_extractor
```

### Step 2: Define Plugin Logic

```python
"""
Todo Extractor Plugin - Extracts action items from conversations.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)


class TodoExtractorPlugin(BasePlugin):
    """Extract and save action items from conversations."""

    SUPPORTED_ACCESS_LEVELS = ['conversation']

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.todo_patterns = [
            r'I need to (.+)',
            r'I should (.+)',
            r'TODO: (.+)',
            r'reminder to (.+)',
        ]

    async def initialize(self):
        if not self.enabled:
            return

        logger.info("TodoExtractor plugin initialized")

    async def on_conversation_complete(self, context: PluginContext):
        try:
            transcript = context.data.get('transcript', '')
            todos = self._extract_todos(transcript)

            if todos:
                await self._save_todos(context.user_id, todos)

                return PluginResult(
                    success=True,
                    message=f"Extracted {len(todos)} action items",
                    data={'todos': todos}
                )

            return PluginResult(success=True, message="No action items found")

        except Exception as e:
            logger.error(f"Error extracting todos: {e}")
            return PluginResult(success=False, message=str(e))

    def _extract_todos(self, transcript: str) -> List[str]:
        """Extract todo items from transcript."""
        todos = []

        for pattern in self.todo_patterns:
            matches = re.findall(pattern, transcript, re.IGNORECASE)
            todos.extend(matches)

        return list(set(todos))  # Remove duplicates

    async def _save_todos(self, user_id: str, todos: List[str]):
        """Save todos to database or external service."""
        from advanced_omi_backend.database import get_database

        db = get_database()
        for todo in todos:
            await db['todos'].insert_one({
                'user_id': user_id,
                'task': todo,
                'completed': False,
                'created_at': datetime.utcnow()
            })
```

### Step 3: Configure Plugin

`config/plugins.yml`:

```yaml
plugins:
  todo_extractor:
    enabled: true
    events:
      - conversation.complete
    condition:
      type: always
```

### Step 4: Test Plugin

1. Restart backend: `docker compose restart`
2. Create a conversation with phrases like "I need to buy milk"
3. Check logs: `docker compose logs -f chronicle-backend | grep TodoExtractor`
4. Verify todos in database

## Configuration

### YAML Configuration

`config/plugins.yml`:

```yaml
plugins:
  my_plugin:
    # Basic Configuration
    enabled: true                 # Enable/disable plugin

    # Event Subscriptions
    events:
      - conversation.complete
      - memory.processed

    # Execution Conditions
    condition:
      type: always                # always, wake_word, regex
      # wake_words: ["hey assistant"]  # For wake_word type
      # pattern: "urgent"              # For regex type

    # Custom Configuration
    api_url: ${MY_API_URL}        # Environment variable
    timeout: 30
    max_retries: 3
```

### Environment Variables

Use `${VAR_NAME}` syntax:

```yaml
api_key: ${MY_API_KEY}
base_url: ${BASE_URL:-http://localhost:8000}  # With default
```

Add to `.env`:

```bash
MY_API_KEY=your-key-here
BASE_URL=https://api.example.com
```

### Condition Types

**Always Execute**:
```yaml
condition:
  type: always
```

**Wake Word** (transcript events only):
```yaml
condition:
  type: wake_word
  wake_words:
    - hey assistant
    - computer
```

**Regex Pattern**:
```yaml
condition:
  type: regex
  pattern: "urgent|important"
```

## Testing Plugins

### Unit Tests

`tests/test_my_plugin.py`:

```python
import pytest
from plugins.my_plugin import MyPlugin
from plugins.base import PluginContext

class TestMyPlugin:
    def test_plugin_initialization(self):
        config = {'enabled': True, 'events': ['conversation.complete']}
        plugin = MyPlugin(config)
        assert plugin.enabled is True

    @pytest.mark.asyncio
    async def test_conversation_processing(self):
        plugin = MyPlugin({'enabled': True})
        await plugin.initialize()

        context = PluginContext(
            user_id='test-user',
            event='conversation.complete',
            data={'transcript': 'Test transcript'}
        )

        result = await plugin.on_conversation_complete(context)
        assert result.success is True
```

### Integration Testing

1. **Enable Test Plugin**:
```yaml
test_event:
  enabled: true
  events:
    - conversation.complete
```

2. **Check Logs**:
```bash
docker compose logs -f | grep "test_event"
```

3. **Upload Test Audio**:
```bash
curl -X POST http://localhost:8000/api/process-audio-files \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@test.wav"
```

### Manual Testing Checklist

- [ ] Plugin loads without errors
- [ ] Configuration validates correctly
- [ ] Events trigger plugin execution
- [ ] Plugin logic executes successfully
- [ ] Errors are handled gracefully
- [ ] Logs provide useful information

## Best Practices

### 1. Error Handling

Always wrap logic in try-except:

```python
async def on_conversation_complete(self, context):
    try:
        # Your logic
        result = await self.process(context)
        return PluginResult(success=True, data=result)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return PluginResult(success=False, message=str(e))
```

### 2. Logging

Use appropriate log levels:

```python
logger.debug("Detailed debug information")
logger.info("Important milestones")
logger.warning("Non-critical issues")
logger.error("Errors that need attention")
```

### 3. Resource Management

Clean up in `cleanup()`:

```python
async def initialize(self):
    self.client = ExternalClient()
    await self.client.connect()

async def cleanup(self):
    if self.client:
        await self.client.disconnect()
```

### 4. Configuration Validation

Validate in `initialize()`:

```python
async def initialize(self):
    if not self.config.get('api_key'):
        raise ValueError("API key is required")

    if self.config.get('timeout', 0) <= 0:
        raise ValueError("Timeout must be positive")
```

### 5. Async Best Practices

Use `asyncio.to_thread()` for blocking operations:

```python
import asyncio

async def my_method(self):
    # Run blocking operation in thread pool
    result = await asyncio.to_thread(blocking_function, arg1, arg2)
    return result
```

### 6. Database Access

Use the global database handle:

```python
from advanced_omi_backend.database import get_database

async def save_data(self, data):
    db = get_database()
    await db['my_collection'].insert_one(data)
```

### 7. LLM Access

Use the global LLM client:

```python
from advanced_omi_backend.llm_client import async_generate

async def generate_summary(self, text):
    prompt = f"Summarize: {text}"
    summary = await async_generate(prompt)
    return summary
```

## Examples

### Example 1: Slack Notifier

```python
class SlackNotifierPlugin(BasePlugin):
    SUPPORTED_ACCESS_LEVELS = ['conversation']

    async def initialize(self):
        self.webhook_url = self.config.get('slack_webhook_url')
        if not self.webhook_url:
            raise ValueError("Slack webhook URL required")

    async def on_conversation_complete(self, context):
        transcript = context.data.get('transcript', '')
        duration = context.data.get('duration', 0)

        message = {
            "text": f"New conversation ({duration:.1f}s)",
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{transcript[:500]}```"}
            }]
        }

        async with aiohttp.ClientSession() as session:
            await session.post(self.webhook_url, json=message)

        return PluginResult(success=True, message="Notification sent")
```

### Example 2: Keyword Alerter

```python
class KeywordAlerterPlugin(BasePlugin):
    SUPPORTED_ACCESS_LEVELS = ['transcript']

    async def on_transcript(self, context):
        transcript = context.data.get('transcript', '')
        keywords = self.config.get('keywords', [])

        for keyword in keywords:
            if keyword.lower() in transcript.lower():
                await self.send_alert(keyword, transcript)
                return PluginResult(
                    success=True,
                    message=f"Alert sent for keyword: {keyword}"
                )

        return PluginResult(success=True)
```

### Example 3: Analytics Tracker

```python
class AnalyticsTrackerPlugin(BasePlugin):
    SUPPORTED_ACCESS_LEVELS = ['conversation', 'memory']

    async def on_conversation_complete(self, context):
        duration = context.data.get('duration', 0)
        word_count = len(context.data.get('transcript', '').split())

        await self.track_event('conversation_complete', {
            'user_id': context.user_id,
            'duration': duration,
            'word_count': word_count,
        })

        return PluginResult(success=True)

    async def on_memory_processed(self, context):
        memory_count = context.data.get('memory_count', 0)

        await self.track_event('memory_processed', {
            'user_id': context.user_id,
            'memory_count': memory_count,
        })

        return PluginResult(success=True)
```

## Troubleshooting

### Plugin Not Loading

**Check logs**:
```bash
docker compose logs chronicle-backend | grep "plugin"
```

**Common issues**:
- Plugin directory name doesn't match class name convention
- Missing `__init__.py` or incorrect exports
- Syntax errors in plugin.py
- Not inheriting from `BasePlugin`

**Solution**:
1. Verify directory structure matches: `plugins/my_plugin/`
2. Class name should be: `MyPluginPlugin`
3. Export in `__init__.py`: `from .plugin import MyPluginPlugin`

### Plugin Enabled But Not Executing

**Check**:
- Plugin enabled in `plugins.yml`
- Correct events subscribed
- Condition matches (wake_word, regex, etc.)

**Debug**:
```python
async def on_conversation_complete(self, context):
    logger.info(f"Plugin executed! Context: {context}")
    # Your logic
```

### Configuration Errors

**Error**: `Environment variable not found`

**Solution**:
- Add variable to `.env` file
- Use default values: `${VAR:-default}`
- Check variable name spelling

### Import Errors

**Error**: `ModuleNotFoundError`

**Solution**:
- Restart backend after adding dependencies
- Verify imports are from correct modules
- Use absolute imports for framework classes: `from advanced_omi_backend.plugins.base import BasePlugin`

### Database Connection Issues

**Error**: `Database connection failed`

**Solution**:
```python
from advanced_omi_backend.database import get_database

async def my_method(self):
    db = get_database()  # Global database handle
    # Use db...
```

## Advanced Topics

### Custom Conditions

Implement custom condition checking:

```python
async def on_conversation_complete(self, context):
    # Custom condition check
    if not self._should_execute(context):
        return PluginResult(success=True, message="Skipped")

    # Your logic
    ...

def _should_execute(self, context):
    # Custom logic
    duration = context.data.get('duration', 0)
    return duration > 60  # Only process long conversations
```

### Plugin Dependencies

Share data between plugins using context metadata:

```python
# Plugin A
async def on_conversation_complete(self, context):
    context.metadata['extracted_keywords'] = ['important', 'urgent']
    return PluginResult(success=True)

# Plugin B (executes after Plugin A)
async def on_conversation_complete(self, context):
    keywords = context.metadata.get('extracted_keywords', [])
    # Use keywords...
```

### External Service Integration

```python
import aiohttp

class ExternalServicePlugin(BasePlugin):
    async def initialize(self):
        self.session = aiohttp.ClientSession()
        self.api_url = self.config.get('api_url')
        self.api_key = self.config.get('api_key')

    async def cleanup(self):
        await self.session.close()

    async def on_conversation_complete(self, context):
        async with self.session.post(
            self.api_url,
            headers={'Authorization': f'Bearer {self.api_key}'},
            json={'transcript': context.data.get('transcript')}
        ) as response:
            result = await response.json()
            return PluginResult(success=True, data=result)
```

## Resources

- **Plugin Framework**: `backends/advanced/src/advanced_omi_backend/plugins/` (base.py, router.py, events.py, services.py)
- **Plugin Implementations**: `plugins/` at repo root
  - Email Summarizer: `plugins/email_summarizer/`
  - Home Assistant: `plugins/homeassistant/`
  - Test Event: `plugins/test_event/`
  - Test Button Actions: `plugins/test_button_actions/`
- **Plugin Generator**: `backends/advanced/scripts/create_plugin.py`
- **Configuration**: `config/plugins.yml.template`

## Contributing Plugins

Want to share your plugin with the community?

1. Create a well-documented plugin
2. Add comprehensive README
3. Include configuration examples
4. Test thoroughly
5. Submit PR to Chronicle repository

## Support

- **GitHub Issues**: [chronicle-ai/chronicle/issues](https://github.com/chronicle-ai/chronicle/issues)
- **Discussions**: [chronicle-ai/chronicle/discussions](https://github.com/chronicle-ai/chronicle/discussions)
- **Documentation**: [Chronicle Docs](https://github.com/chronicle-ai/chronicle)

Happy plugin development! 🚀
