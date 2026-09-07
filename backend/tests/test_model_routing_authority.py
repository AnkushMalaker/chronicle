"""The model registry is the sole authority for LLM routing."""

import pytest
from omegaconf import OmegaConf

from backend.model_registry import (
    AppModels,
    LLMOperationConfig,
    ModelDef,
    validate_model_routing_authority,
)


def _registry() -> AppModels:
    primary = ModelDef(
        name="primary",
        model_type="llm",
        model_provider="openrouter",
        model_name="provider/primary",
        model_url="https://openrouter.ai/api/v1",
    )
    fast = ModelDef(
        name="fast",
        model_type="llm",
        model_provider="openrouter",
        model_name="provider/fast",
        model_url="https://openrouter.ai/api/v1",
    )
    special = ModelDef(
        name="special",
        model_type="llm",
        model_provider="openrouter",
        model_name="provider/special",
        model_url="https://openrouter.ai/api/v1",
    )
    return AppModels(
        defaults={"llm": "primary", "fast_llm": "fast"},
        models={model.name: model for model in (primary, fast, special)},
        llm_operations={
            "chat": LLMOperationConfig(),
            "followup_resolution": LLMOperationConfig(),
            "plugin_assistant": LLMOperationConfig(),
            "memory_write": LLMOperationConfig(model="special"),
        },
    )


def test_effective_routing_reports_role_default_and_explicit_override():
    registry = _registry()

    assert registry.explain_llm_operation("chat") == {
        "model": "primary",
        "model_name": "provider/primary",
        "provider": "openrouter",
        "role": "llm",
        "source": "defaults.llm",
    }
    assert registry.explain_llm_operation("followup_resolution") == {
        "model": "fast",
        "model_name": "provider/fast",
        "provider": "openrouter",
        "role": "fast_llm",
        "source": "defaults.fast_llm",
    }
    assert registry.explain_llm_operation("plugin_assistant")["source"] == (
        "defaults.fast_llm"
    )
    assert registry.explain_llm_operation("memory_write") == {
        "model": "special",
        "model_name": "provider/special",
        "provider": "openrouter",
        "role": None,
        "source": "llm_operations.memory_write.model",
    }


@pytest.mark.parametrize(
    ("config", "path"),
    [
        (
            {"memory": {"backends": {"pi": {"model": "primary"}}}},
            "memory.backends.pi.model",
        ),
        ({"timeline": {"pi": {"model": "provider/primary"}}}, "timeline.pi.model"),
    ],
)
def test_executor_model_pins_are_rejected(config, path):
    with pytest.raises(ValueError, match=path.replace(".", r"\.")):
        validate_model_routing_authority(OmegaConf.create(config))
