"""Reasoning parameter compatibility for versioned GPT-5 models."""

from advanced_omi_backend.model_registry import ModelDef, ResolvedLLMOperation


def _operation(model_name: str) -> ResolvedLLMOperation:
    return ResolvedLLMOperation(
        model_def=ModelDef(
            name="test",
            model_name=model_name,
            model_type="llm",
            model_provider="openai",
            model_url="https://api.openai.com/v1",
        ),
        temperature=0.2,
        max_tokens=100,
        reasoning_effort="none",
    )


def test_gpt_5_4_preserves_none_reasoning_effort():
    assert _operation("gpt-5.4-mini").to_api_params()["reasoning_effort"] == "none"


def test_unversioned_gpt_5_uses_minimal_reasoning_effort():
    assert _operation("gpt-5-mini").to_api_params()["reasoning_effort"] == "minimal"
