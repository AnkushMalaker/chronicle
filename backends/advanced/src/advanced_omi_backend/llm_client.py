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
from advanced_omi_backend.openai_factory import create_openai_client, is_langfuse_enabled
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
    def health_check(self) -> Dict:
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
            raise ValueError(f"LLM configuration incomplete: api_key={'set' if self.api_key else 'MISSING'}, base_url={'set' if self.base_url else 'MISSING'}, model={'set' if self.model else 'MISSING'}")

        # Initialize OpenAI client with optional Langfuse tracing
        try:
            self.client = create_openai_client(
                api_key=self.api_key, base_url=self.base_url, is_async=False
            )
            self.logger.info(f"OpenAI client initialized, base_url: {self.base_url}")
        except ImportError:
            self.logger.error("OpenAI library not installed. Install with: pip install openai")
            raise
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenAI client: {e}")
            raise

    def generate(
        self, prompt: str, model: str | None = None, temperature: float | None = None,
        **langfuse_kwargs,
    ) -> str:
        """Generate text completion using OpenAI-compatible API."""
        try:
            model_name = model or self.model
            temp = temperature if temperature is not None else self.temperature

            params = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temp,
            }
            if is_langfuse_enabled():
                params.update(langfuse_kwargs)

            response = self.client.chat.completions.create(**params)
            return response.choices[0].message.content.strip()
        except Exception as e:
            self.logger.error(f"Error generating completion: {e}")
            raise

    def chat_with_tools(
        self, messages: list, tools: list | None = None, model: str | None = None,
        temperature: float | None = None, **langfuse_kwargs,
    ):
        """Chat completion with tool/function calling support. Returns raw response object."""
        model_name = model or self.model
        params = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if tools:
            params["tools"] = tools
        if is_langfuse_enabled():
            params.update(langfuse_kwargs)
        return self.client.chat.completions.create(**params)

    def health_check(self) -> Dict:
        """Check OpenAI-compatible service health."""
        try:
            # For OpenAI API, check if we have valid configuration
            # Avoid calling /models endpoint as it can be unreliable
            if self.api_key and self.api_key != "dummy" and self.model:
                return {
                    "status": "✅ Connected",
                    "base_url": self.base_url,
                    "default_model": self.model,
                    "api_key_configured": bool(self.api_key and self.api_key != "dummy"),
                }
            else:
                return {
                    "status": "⚠️ Configuration incomplete",
                    "base_url": self.base_url,
                    "default_model": self.model,
                    "api_key_configured": bool(self.api_key and self.api_key != "dummy"),
                }
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return {
                "status": "❌ Failed",
                "error": str(e),
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
                logger.info(f"Creating LLM client from registry: {llm_def.name} ({llm_def.model_provider})")
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


def _langfuse_metadata(session_id: str | None) -> dict:
    """Return metadata dict with langfuse_session_id if Langfuse is enabled."""
    if session_id and is_langfuse_enabled():
        return {"langfuse_session_id": session_id}
    return {}


# Async wrapper for blocking LLM operations
async def async_generate(
    prompt: str,
    model: str | None = None,
    temperature: float | None = None,
    operation: str | None = None,
    langfuse_session_id: str | None = None,
) -> str:
    """Async wrapper for LLM text generation.

    When ``operation`` is provided, parameters are resolved from the
    ``llm_operations`` config section via ``get_llm_operation()``.
    The resolved config determines model, temperature, max_tokens, etc.
    Explicit ``model``/``temperature`` kwargs still override the resolved values.

    When ``langfuse_session_id`` is provided and Langfuse is enabled,
    the session ID is set on the current Langfuse trace to group all
    LLM calls for a conversation.
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
            api_params["messages"] = [{"role": "user", "content": prompt}]
            api_params["metadata"] = _langfuse_metadata(langfuse_session_id)
            response = await client.chat.completions.create(**api_params)
            return response.choices[0].message.content.strip()

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
    langfuse_session_id: str | None = None,
):
    """Async wrapper for chat completion with tool calling.

    When ``operation`` is provided, parameters are resolved from config.
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
            api_params["metadata"] = _langfuse_metadata(langfuse_session_id)
            return await client.chat.completions.create(**api_params)

    # Fallback: use singleton client
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: client.chat_with_tools(messages, tools, model, temperature)
    )


async def async_health_check() -> Dict:
    """Async wrapper for LLM health check."""
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, client.health_check)
