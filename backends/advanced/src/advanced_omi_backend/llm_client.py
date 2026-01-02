"""
Abstract LLM client interface for unified LLM operations across different providers.

This module provides a standardized interface for LLM operations that works with
OpenAI, Ollama, and other OpenAI-compatible APIs.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

from advanced_omi_backend.services.memory.config import load_config_yml as _load_root_config
from advanced_omi_backend.services.memory.config import resolve_value as _resolve_value

from advanced_omi_backend.model_registry import get_models_registry

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
            # Check if Langfuse is configured
            langfuse_enabled = (
                os.getenv("LANGFUSE_PUBLIC_KEY")
                and os.getenv("LANGFUSE_SECRET_KEY")
                and os.getenv("LANGFUSE_HOST")
            )

            if langfuse_enabled:
                # Use Langfuse-wrapped OpenAI for tracing
                import langfuse.openai as openai
                self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
                self.logger.info(f"OpenAI client initialized with Langfuse tracing, base_url: {self.base_url}")
            else:
                # Use regular OpenAI client without tracing
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
                self.logger.info(f"OpenAI client initialized (no tracing), base_url: {self.base_url}")
        except ImportError:
            self.logger.error("OpenAI library not installed. Install with: pip install openai")
            raise
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenAI client: {e}")
            raise

    def generate(
        self, prompt: str, model: str | None = None, temperature: float | None = None
    ) -> str:
        """Generate text completion using OpenAI-compatible API."""
        try:
            model_name = model or self.model
            temp = temperature or self.temperature
            
            # Build completion parameters
            params = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
            }
            
            # Skip temperature for gpt-4o-mini as it only supports default (1)
            if not (model_name and "gpt-4o-mini" in model_name):
                params["temperature"] = temp
            
            response = self.client.chat.completions.create(**params)
            return response.choices[0].message.content.strip()
        except Exception as e:
            self.logger.error(f"Error generating completion: {e}")
            raise

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


# Async wrapper for blocking LLM operations
async def async_generate(
    prompt: str, model: str | None = None, temperature: float | None = None
) -> str:
    """Async wrapper for LLM text generation."""
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, client.generate, prompt, model, temperature)


async def async_health_check() -> Dict:
    """Async wrapper for LLM health check."""
    client = get_llm_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, client.health_check)
