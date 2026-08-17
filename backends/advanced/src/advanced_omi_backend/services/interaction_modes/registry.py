"""In-process registry for plugin-owned interaction modes."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional

from .contracts import InteractionModeDefinition

_LEADING_HERMES = re.compile(r"^(?:(?:hey+|हे)\s+)?(?:hermes|हर्मेश|हरमिस)(?:\s+|$)")


def normalize_interaction_text(text: str, *, strip_hermes: bool = True) -> str:
    """Normalize ASR text while retaining word boundaries for prefix matching."""
    normalized = (
        (text or "")
        .lower()
        .translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if strip_hermes:
        normalized = _LEADING_HERMES.sub("", normalized, count=1).strip()
    return normalized


@dataclass(frozen=True)
class InteractionMatch:
    definition: InteractionModeDefinition
    owner_plugin_id: str
    activation_phrase: str
    remainder: str


class InteractionRegistry:
    """Validates activation phrases and resolves utterances to one mode."""

    def __init__(self) -> None:
        self._modes: dict[str, tuple[str, InteractionModeDefinition]] = {}
        self._phrases: dict[str, str] = {}

    @property
    def modes(self) -> dict[str, tuple[str, InteractionModeDefinition]]:
        return dict(self._modes)

    def register(
        self, owner_plugin_id: str, definition: InteractionModeDefinition
    ) -> None:
        if definition.mode_id in self._modes:
            existing_owner = self._modes[definition.mode_id][0]
            raise ValueError(
                f"interaction mode '{definition.mode_id}' is already owned by "
                f"plugin '{existing_owner}'"
            )

        normalized_phrases: list[str] = []
        for raw_phrase in definition.activation_phrases:
            phrase = normalize_interaction_text(raw_phrase)
            if not phrase:
                raise ValueError(
                    f"interaction mode '{definition.mode_id}' has an empty activation phrase"
                )
            for existing_phrase, existing_mode_id in self._phrases.items():
                if (
                    phrase == existing_phrase
                    or phrase.startswith(f"{existing_phrase} ")
                    or existing_phrase.startswith(f"{phrase} ")
                ):
                    raise ValueError(
                        f"activation phrase '{phrase}' for mode '{definition.mode_id}' "
                        f"collides with '{existing_phrase}' for mode '{existing_mode_id}'"
                    )
            normalized_phrases.append(phrase)

        self._modes[definition.mode_id] = (owner_plugin_id, definition)
        for phrase in normalized_phrases:
            self._phrases[phrase] = definition.mode_id

    def match(self, text: str) -> Optional[InteractionMatch]:
        normalized = normalize_interaction_text(text)
        for phrase in sorted(self._phrases, key=len, reverse=True):
            if normalized == phrase or normalized.startswith(f"{phrase} "):
                mode_id = self._phrases[phrase]
                owner, definition = self._modes[mode_id]
                remainder = normalized[len(phrase) :].strip()
                return InteractionMatch(
                    definition=definition,
                    owner_plugin_id=owner,
                    activation_phrase=phrase,
                    remainder=remainder,
                )
        return None

    def get(self, mode_id: str) -> Optional[tuple[str, InteractionModeDefinition]]:
        return self._modes.get(mode_id)
