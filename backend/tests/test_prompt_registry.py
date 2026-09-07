import threading

import pytest

from backend.prompt_registry import PromptRegistry


class _Prompt:
    def __init__(self, template: str, *, is_fallback: bool = False):
        self.template = template
        self.is_fallback = is_fallback

    def compile(self, **variables):
        value = self.template
        for key, replacement in variables.items():
            value = value.replace(f"{{{{{key}}}}}", str(replacement))
        return value


@pytest.mark.asyncio
async def test_prompt_registry_caches_remote_template_but_compiles_fresh_variables():
    registry = PromptRegistry(cache_ttl_seconds=300)
    calls = []

    class Client:
        def get_prompt(self, prompt_id, *, fallback):
            calls.append((prompt_id, fallback))
            return _Prompt("Remote {{value}}")

    registry._langfuse = Client()
    registry._client_initialized = True
    registry.register_default("example", "Default {{value}}")

    first = await registry.get_prompt("example", value="one")
    second = await registry.get_prompt("example", value="two")

    assert first == "Remote one"
    assert second == "Remote two"
    assert calls == [("example", "Default {{value}}")]


@pytest.mark.asyncio
async def test_prompt_registry_cools_down_after_remote_failure():
    registry = PromptRegistry(failure_cooldown_seconds=300)
    calls = []

    class Client:
        def get_prompt(self, prompt_id, *, fallback):
            calls.append(prompt_id)
            raise OSError("host unavailable")

    registry._langfuse = Client()
    registry._client_initialized = True
    registry.register_default("example", "Default {{value}}")

    first = await registry.get_prompt("example", value="one")
    second = await registry.get_prompt("example", value="two")

    assert first == "Default one"
    assert second == "Default two"
    assert calls == ["example"]


@pytest.mark.asyncio
async def test_prompt_registry_cools_down_langfuse_fallback_response():
    registry = PromptRegistry(cache_ttl_seconds=0, failure_cooldown_seconds=300)
    calls = []

    class Client:
        def get_prompt(self, prompt_id, *, fallback):
            calls.append(prompt_id)
            return _Prompt(fallback, is_fallback=True)

    registry._langfuse = Client()
    registry._client_initialized = True
    registry.register_default("example", "Default {{value}}")

    first = await registry.get_prompt("example", value="one")
    second = await registry.get_prompt("example", value="two")

    assert first == "Default one"
    assert second == "Default two"
    assert calls == ["example"]


@pytest.mark.asyncio
async def test_prompt_registry_fetch_does_not_block_async_event_loop():
    registry = PromptRegistry(cache_ttl_seconds=300)
    caller_thread = threading.get_ident()
    fetch_threads = []

    class Client:
        def get_prompt(self, prompt_id, *, fallback):
            fetch_threads.append(threading.get_ident())
            return _Prompt(fallback)

    registry._langfuse = Client()
    registry._client_initialized = True
    registry.register_default("example", "Default")

    assert await registry.get_prompt("example") == "Default"
    assert fetch_threads and fetch_threads[0] != caller_thread
