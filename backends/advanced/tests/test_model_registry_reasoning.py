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


def test_openrouter_qualified_gpt_5_6_uses_reasoning_parameters():
    params = _operation("openai/gpt-5.6-luna").to_api_params()

    assert params["model"] == "openai/gpt-5.6-luna"
    assert params["max_completion_tokens"] == 100
    assert params["reasoning_effort"] == "none"
    assert "max_tokens" not in params
    assert "temperature" not in params


def test_muse_sampling_and_reasoning_prompt_follow_model_card():
    operation = ResolvedLLMOperation(
        model_def=ModelDef(
            name="muse-glimmer-llm",
            model_name="meta-models/Muse-Glimmer-30B-GGUF:kquant-17gb",
            model_type="llm",
            model_provider="llamacpp",
            model_url="http://llama-cpp-llm:8080/v1",
            thinking=True,
            capabilities=["vision"],
            system_prompt_prefix="Reasoning strength: high",
            model_params={"temperature": 1.0, "top_p": 0.95, "top_k": 64},
        ),
        temperature=1.0,
        max_tokens=4096,
        reasoning_effort="high",
    )

    params = operation.to_api_params()
    assert params["temperature"] == 1.0
    assert params["top_p"] == 0.95
    assert params["extra_body"] == {
        "top_k": 64,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    original = [{"role": "system", "content": "Use Chronicle tools."}]
    prepared = operation.prepare_messages(original)
    assert prepared == [
        {
            "role": "system",
            "content": "Reasoning strength: high\n\nUse Chronicle tools.",
        }
    ]
    assert original == [{"role": "system", "content": "Use Chronicle tools."}]
