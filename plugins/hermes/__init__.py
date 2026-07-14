"""
Hermes Agent plugin for Chronicle.

Routes voice commands prefixed with the "hermes" keyword to an external
Hermes agent over its OpenAI-compatible HTTP API and records the reply.
"""

from .plugin import HermesPlugin

__all__ = ["HermesPlugin"]
