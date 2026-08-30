"""The single seam for resolving Main and isolated memory-space vaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID

from advanced_omi_backend.models.memory_space import MemorySpace


class MemoryScopeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryScope:
    user_id: str
    memory_space_id: Optional[str] = None

    @property
    def is_main(self) -> bool:
        return self.memory_space_id is None


class MemoryScopeResolver:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir or os.getenv("DATA_DIR", "/app/data"))

    def main_root(self, user_id: str) -> Path:
        return (
            self.data_dir
            / "conversation_docs"
            / self._safe_component(user_id, "user_id")
        )

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        value = str(value)
        if not value or value in {".", ".."} or Path(value).name != value:
            raise MemoryScopeError(f"Invalid {label}")
        return value

    @classmethod
    def _space_component(cls, value: Optional[str]) -> str:
        candidate = cls._safe_component(value or "", "memory_space_id")
        try:
            return str(UUID(candidate))
        except ValueError as exc:
            raise MemoryScopeError("Invalid memory_space_id") from exc

    def space_base(self, scope: MemoryScope) -> Path:
        if scope.is_main:
            raise MemoryScopeError("Main does not have an isolated space directory")
        return (
            self.data_dir
            / "memory_spaces"
            / self._safe_component(scope.user_id, "user_id")
            / self._space_component(scope.memory_space_id)
        )

    def vault_root(self, scope: MemoryScope) -> Path:
        return (
            self.main_root(scope.user_id)
            if scope.is_main
            else self.space_base(scope) / "vault"
        )

    def baseline_root(self, scope: MemoryScope) -> Path:
        return self.space_base(scope) / "baseline"

    async def require_space(
        self,
        scope: MemoryScope,
        *,
        writable: bool = False,
        allow_merging: bool = False,
    ) -> MemorySpace:
        if scope.is_main:
            raise MemoryScopeError("An isolated memory space is required")
        space = await MemorySpace.find_one(
            MemorySpace.space_id == scope.memory_space_id,
            MemorySpace.user_id == scope.user_id,
        )
        if space is None:
            raise MemoryScopeError("Memory space not found")
        writable_states = {"active", "merging"} if allow_merging else {"active"}
        if writable and space.state not in writable_states:
            raise MemoryScopeError(f"Memory space is {space.state}, not active")
        return space


def memory_scope(user_id: str, memory_space_id: Optional[str] = None) -> MemoryScope:
    return MemoryScope(str(user_id), memory_space_id or None)
