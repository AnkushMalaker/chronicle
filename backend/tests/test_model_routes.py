from backend.model_registry import AppModels, LLMOperationConfig, ModelDef
from backend.model_routes import (
    effective_model_routes,
    effective_operation_routes,
    format_model_routes,
    public_endpoint,
)


def _registry() -> AppModels:
    qwen = ModelDef(
        name="qwen3.8-llm",
        description="Local Qwen",
        model_type="llm",
        model_provider="llamacpp",
        api_family="openai",
        model_name="qwen-local.gguf",
        model_url="http://llama-cpp-llm:8080/v1",
        capabilities=["vision"],
    )
    return AppModels(
        defaults={"llm": qwen.name, "fast_llm": qwen.name, "fallback_llm": qwen.name},
        models={qwen.name: qwen},
        llm_operations={
            name: LLMOperationConfig(model=qwen.name)
            for name in (
                "memory_write",
                "memory_search",
                "timeline_segmentation",
                "timeline_thumbnail",
                "immich_visual_evidence",
                "manual_memory_image",
            )
        },
    )


def _config() -> dict:
    return {
        "memory": {
            "agents": {
                "write": {"backend": "pi"},
                "search": {"backend": "pi"},
            }
        },
        "manual_memories": {
            "agents": {
                "analyze_image": {
                    "backend": "model",
                    "operation": "manual_memory_image",
                }
            }
        },
        "timeline": {
            "executor": "pi",
            "pi": {"operation": "timeline_segmentation"},
            "thumbnails": {
                "backend": "model",
                "operation": "timeline_thumbnail",
            },
            "immich_visual_evidence": {
                "backend": "model",
                "operation": "immich_visual_evidence",
            },
        },
    }


def test_effective_routes_make_every_selected_runtime_model_visible():
    routes = effective_model_routes(_config(), _registry())
    indexed = {route["workload"]: route for route in routes}

    assert indexed["timeline.segmentation"] == {
        "workload": "timeline.segmentation",
        "adapter": "pi",
        "operation": "timeline_segmentation",
        "model": "qwen3.8-llm",
        "model_name": "qwen-local.gguf",
        "provider": "llamacpp",
        "endpoint": "http://llama-cpp-llm:8080/v1",
        "location": "self-hosted",
        "source": "llm_operations.timeline_segmentation.model",
        "max_tokens": None,
        "reasoning_effort": None,
        "requested_reasoning_effort": None,
        "reasoning_policy": "provider",
        "response_format": "text",
    }
    assert indexed["timeline.immich_visual_evidence"]["model"] == "qwen3.8-llm"
    assert indexed["timeline.thumbnail"]["location"] == "self-hosted"
    assert indexed["manual_memories.image"]["adapter"] == "model"
    assert indexed["memory.write"]["operation"] == "memory_write"


def test_codex_selection_is_preserved_but_cannot_hide_from_report():
    config = _config()
    config["timeline"] = {
        **config["timeline"],
        "executor": "codex",
        "codex": {"model": "gpt-5.6-luna"},
    }

    route = next(
        item
        for item in effective_model_routes(config, _registry())
        if item["workload"] == "timeline.segmentation"
    )

    assert route["adapter"] == "codex"
    assert route["model"] == "gpt-5.6-luna"
    assert route["provider"] == "codex"
    assert route["location"] == "external"
    assert route["source"] == "timeline.codex.model"


def test_plain_text_report_prints_provider_location_and_selection_source():
    report = format_model_routes(effective_model_routes(_config(), _registry()))

    assert "timeline.segmentation" in report
    assert "qwen3.8-llm" in report
    assert "self-hosted" in report
    assert "http://llama-cpp-llm:8080/v1" in report
    assert "llm_operations.timeline_segmentation.model" in report


def test_complete_operation_audit_cannot_hide_an_external_island():
    routes = effective_operation_routes(_registry())

    assert len(routes) == 6
    assert {route["location"] for route in routes} == {"self-hosted"}
    assert {route["operation"] for route in routes} == {
        "memory_write",
        "memory_search",
        "timeline_segmentation",
        "timeline_thumbnail",
        "immich_visual_evidence",
        "manual_memory_image",
    }


def test_public_endpoint_removes_userinfo_query_and_fragment():
    endpoint = "https://user:password@example.com:8443/v1?api_key=secret#private"

    assert public_endpoint(endpoint) == "https://example.com:8443/v1"
    assert "password" not in public_endpoint(endpoint)
    assert "secret" not in public_endpoint(endpoint)
