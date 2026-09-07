import pytest

from backend.model_registry import ModelDef
from backend.routers.modules import openai_compat_routes as routes


@pytest.fixture(autouse=True)
def clear_unavailable_models():
    routes._unavailable_models.clear()
    yield
    routes._unavailable_models.clear()


def test_unavailable_model_is_bypassed_until_cooldown_expires(monkeypatch):
    now = 1_000.0
    monkeypatch.setattr(routes.time, "monotonic", lambda: now)

    routes._mark_model_unavailable("llamacpp-llm")

    assert routes._model_is_unavailable("llamacpp-llm")

    now += routes._UPSTREAM_FAILURE_COOLDOWN_SECONDS + 0.1
    assert not routes._model_is_unavailable("llamacpp-llm")


@pytest.mark.asyncio
async def test_proxy_skips_a_model_with_an_open_circuit():
    model = ModelDef(
        name="llamacpp-llm",
        model_type="llm",
        model_url="http://unreachable.example/v1",
    )
    routes._mark_model_unavailable(model.name)

    with pytest.raises(routes._UpstreamTransportError, match="cooldown active"):
        await routes._proxy_chat_completion(model, {"messages": []}, stream=False)
