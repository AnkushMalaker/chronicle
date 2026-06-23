"""
Button Configuration plugin for Chronicle.

Maps device button presses (single / double) to configurable system actions —
stop the current TTS playback, close the current conversation, star it, or call
another plugin. The mapping lives in config.yml so the same firmware button can
be re-purposed without a code change.
"""

from .plugin import ButtonControlPlugin

__all__ = ["ButtonControlPlugin"]
