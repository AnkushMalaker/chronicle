"""Long-lived, voice-driven interaction modes.

Interaction modes are operational sessions (shopping, dictation, guided setup,
and similar workflows).  They deliberately do not own or require a semantic
Conversation: the audio pipeline may create, reconcile, split, or merge
Conversations independently while a mode remains active.
"""

from .contracts import (
    InteractionContext,
    InteractionInput,
    InteractionModeDefinition,
    InteractionResult,
    InteractionSession,
)
from .ingress import InteractionIngress, InteractionIngressResult
from .registry import InteractionMatch, InteractionRegistry
from .store import InteractionStore

__all__ = [
    "InteractionContext",
    "InteractionIngress",
    "InteractionIngressResult",
    "InteractionInput",
    "InteractionMatch",
    "InteractionModeDefinition",
    "InteractionRegistry",
    "InteractionResult",
    "InteractionSession",
    "InteractionStore",
]
