"""Chronicle memory agent package.

Public symbols are loaded lazily so low-level deterministic vault helpers can import
``agent.section_edit`` without initializing the LLM agents (or creating cycles with
``vault_tools``).
"""

from importlib import import_module

_EXPORTS = {
    "CodexMemoryAgent": (".codex_agent", "CodexMemoryAgent"),
    "codex_executor_available": (".codex_agent", "codex_executor_available"),
    "MemoryAgent": (".memory_agent", "MemoryAgent"),
    "MemoryAgentResult": (".memory_agent", "MemoryAgentResult"),
    "PiMemoryAgent": (".pi_agent", "PiMemoryAgent"),
    "pi_executor_available": (".pi_agent", "pi_executor_available"),
    "search_vault": (".memory_agent", "search_vault"),
    "search_vault_with_pi": (".pi_agent", "search_vault_with_pi"),
    "VaultTools": (".vault_tools", "VaultTools"),
    "VaultToolError": (".vault_tools", "VaultToolError"),
    "VAULT_TOOL_SCHEMAS": (".vault_tools", "VAULT_TOOL_SCHEMAS"),
    "VAULT_SEARCH_TOOL_SCHEMAS": (".vault_tools", "VAULT_SEARCH_TOOL_SCHEMAS"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
