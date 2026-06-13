"""
Abstract LLM client interface for unified LLM operations across different providers.

This module provides a standardized interface for LLM operations that works with
OpenAI, Ollama, and other OpenAI-compatible APIs.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from advanced_omi_backend.model_registry import get_models_registry
from advanced_omi_backend.openai_factory import (
    create_openai_client,
    model_supports_temperature,
)
from advanced_omi_backend.services.memory.config import (
    load_config_yml as _load_root_config,
)
from advanced_omi_backend.services.memory.config import resolve_value as _resolve_value

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self, model: str | None = None, temperature: float = 0.1):
        self.model = model
        self.temperature = temperature
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def generate(
        self, prompt: str, model: str | None = None, temperature: float | None = None
    ) -> str:
        """Generate text completion from prompt."""
        pass

    @abstractmethod
    async def health_check(self) -> Dict:
        """Check if the LLM service is available and healthy."""
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this client."""
        pass


class OpenAILLMClient(LLMClient):
    """OpenAI-compatible LLM client that works with OpenAI, Ollama, and other compatible APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
    ):
        super().__init__(model, temperature)
        # Do not read from environment here; values are provided by config.yml
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        if not self.api_key or not self.base_url or not self.model:
            raise ValueError(
                f"LLM configuration incomplete: api_key={'set' if self.api_key else 'MISSING'}, base_url={'set' if self.base_url else 'MISSING'}, model={'set' if self.model else 'MISSING'}"
            )

        # Initialize OpenAI client with optional Langfuse tracing
        try:
            self.client = create_openai_client(
                api_key=self.api_key, base_url=self.base_url, is_async=False
            )
            self.logger.info(f"OpenAI client initialized, base_url: {self.base_url}")
        except ImportError:
            self.logger.error(
                "OpenAI library not installed. Install with: pip install openai"
            )
            raise
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenAI client: {e}")
            raise

    def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        """Generate text completion using OpenAI-compatible API."""
        try:
            model_name = model or self.model
            temp = temperature if temperature is not None else self.temperature

            params: dict[str, Any] = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
            }
            if model_supports_temperature(model_name):
                params["temperature"] = temp
            response = self.client.chat.completions.create(**params)
            return response.choices[0].message.content.strip()
        except Exception as e:
            self.logger.error(f"Error generating completion: {e}")
            raise

    def chat_with_tools(
        self,
        messages: list,
        tools: list | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ):
        """Chat completion with tool/function calling support. Returns raw response object."""
        model_name = model or self.model
        params: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
        }
        if model_supports_temperature(model_name):
            params["temperature"] = (
                temperature if temperature is not None else self.temperature
            )
        if tools:
            params["tools"] = tools
        return self.client.chat.completions.create(**params)

    async def health_check(self) -> Dict:
        """Check OpenAI-compatible service health by calling models.list()."""
        import openai as _openai

        if not self.api_key or not self.base_url or not self.model:
            return {
                "status": "⚠️ Configuration incomplete",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }

        try:
            async_client = create_openai_client(
                api_key=self.api_key, base_url=self.base_url, is_async=True
            )
            await asyncio.wait_for(async_client.models.list(), timeout=5.0)
            return {
                "status": "✅ Connected",
                "healthy": True,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except _openai.AuthenticationError:
            return {
                "status": "❌ Auth Failed — check API key",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except asyncio.TimeoutError:
            return {
                "status": "❌ Connection Timeout",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except _openai.APIConnectionError:
            return {
                "status": "❌ Connection Failed — service unreachable",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return {
                "status": f"❌ Error: {e}",
                "healthy": False,
                "base_url": self.base_url,
                "default_model": self.model,
            }

    def get_default_model(self) -> str:
        """Get the default model for this client."""
        return self.model or "gpt-4o-mini"


class LLMClientFactory:
    """Factory for creating LLM clients based on configuration registry."""

    @staticmethod
    def create_client() -> LLMClient:
        """Create an LLM client based on model registry configuration (config.yml)."""
        registry = get_models_registry()

        if registry:
            llm_def = registry.get_default("llm")
            if llm_def:
                logger.info(
                    f"Creating LLM client from registry: {llm_def.name} ({llm_def.model_provider})"
                )
                params = llm_def.model_params or {}
                return OpenAILLMClient(
                    api_key=llm_def.api_key,
                    base_url=llm_def.model_url,
                    model=llm_def.model_name,
                    temperature=params.get("temperature", 0.1),
                )

        raise ValueError("No default LLM defined in config.yml")

    @staticmethod
    def get_supported_providers() -> list:
        """Get list of supported LLM providers."""
        return ["openai", "ollama"]


# Global LLM client instance
_llm_client = None


def get_llm_client() -> LLMClient:
    """Get the global LLM client instance (singleton pattern)."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClientFactory.create_client()
    return _llm_client


def reset_llm_client():
    """Reset the global LLM client instance (useful for testing)."""
    global _llm_client
    _llm_client = None


# Async wrapper for blocking LLM operations
async def async_generate(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    operation: str | None = None,
    default_model_type: str = "llm",
) -> str:
    """Async wrapper for LLM text generation.

    When ``operation`` is provided, parameters are resolved from the
    ``llm_operations`` config section via ``get_llm_operation()``.
    The resolved config determines model, temperature, max_tokens, etc.
    Explicit ``model``/``temperature`` kwargs still override the resolved values.

    Tracing is handled automatically by the OTEL instrumentor; use
    ``set_otel_session()`` at job boundaries to group calls by session.
    """
    if operation:
        registry = get_models_registry()
        if registry:
            op = registry.get_llm_operation(
                operation, default_model_type=default_model_type
            )
            client = op.get_client(is_async=True)
            api_params = op.to_api_params()
            if temperature is not None:
                api_params["temperature"] = temperature
            if model is not None:
                api_params["model"] = model
            if not model_supports_temperature(api_params.get("model")):
                api_params.pop("temperature", None)
            api_params["messages"] = [{"role": "user", "content": prompt}]
            response = await client.chat.completions.create(**api_params)
            choice = response.choices[0]
            content = (choice.message.content or "").strip()
            if not content:
                # Reasoning models can consume the whole completion budget on
                # reasoning and return empty content with finish_reason=length.
                raise RuntimeError(
                    f"LLM returned empty content for operation {operation!r} "
                    f"(model={api_params.get('model')}, "
                    f"finish_reason={choice.finish_reason}) — if 'length', "
                    f"raise the operation's max_tokens"
                )
            return content

    # Fallback: use singleton client
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: client.generate(prompt, model, temperature)
    )


async def async_chat_with_tools(
    messages: list,
    tools: list | None = None,
    model: str | None = None,
    temperature: float | None = None,
    operation: str | None = None,
):
    """Async wrapper for chat completion with tool calling.

    When ``operation`` is provided, parameters are resolved from config.
    Tracing is handled automatically by the OTEL instrumentor.
    """
    if operation:
        registry = get_models_registry()
        if registry:
            op = registry.get_llm_operation(operation)
            client = op.get_client(is_async=True)
            api_params = op.to_api_params()
            if temperature is not None:
                api_params["temperature"] = temperature
            if model is not None:
                api_params["model"] = model
            api_params["messages"] = messages
            if tools:
                api_params["tools"] = tools
            return await client.chat.completions.create(**api_params)

    # Fallback: use singleton client
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: client.chat_with_tools(messages, tools, model, temperature)
    )


async def async_health_check() -> Dict:
    """Async LLM health check."""
    client = get_llm_client()
    return await client.health_check()


async def async_health_check_fast() -> Optional[Dict]:
    """Health check for a SEPARATE fast LLM, or None if none is configured.

    Returns None when ``defaults.fast_llm`` is unset or points at the same model
    as ``defaults.llm`` (fast tasks reuse the main LLM, already covered by
    :func:`async_health_check`).
    """
    import openai as _openai

    registry = get_models_registry()
    if not registry:
        return None
    fast_name = registry.defaults.get("fast_llm")
    if not fast_name or fast_name == registry.defaults.get("llm"):
        return None
    fast = registry.get_by_name(fast_name)
    if not fast:
        return None

    result = {"base_url": fast.model_url, "default_model": fast.model_name}
    if not fast.api_key or not fast.model_url or not fast.model_name:
        return {**result, "status": "⚠️ Configuration incomplete", "healthy": False}
    try:
        client = create_openai_client(
            api_key=fast.api_key, base_url=fast.model_url, is_async=True
        )
        await asyncio.wait_for(client.models.list(), timeout=5.0)
        return {**result, "status": "✅ Connected", "healthy": True}
    except _openai.AuthenticationError:
        return {**result, "status": "❌ Auth Failed — check API key", "healthy": False}
    except asyncio.TimeoutError:
        return {**result, "status": "❌ Connection Timeout", "healthy": False}
    except _openai.APIConnectionError:
        return {
            **result,
            "status": "❌ Connection Failed — service unreachable",
            "healthy": False,
        }
    except Exception as e:  # noqa: BLE001
        logger.error(f"Fast LLM health check failed: {e}")
        return {**result, "status": f"❌ Error: {e}", "healthy": False}
