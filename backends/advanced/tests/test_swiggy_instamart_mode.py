"""Safety and state-machine tests for the Instamart voice mode."""

import asyncio
import copy
import importlib
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from advanced_omi_backend import llm_client
from advanced_omi_backend.integrations.swiggy import Bucket, SwiggyError
from advanced_omi_backend.services.interaction_modes import (
    InteractionContext,
    InteractionInput,
    InteractionRegistry,
    InteractionSession,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins"
sys.path.insert(0, str(PLUGIN_ROOT)) if str(PLUGIN_ROOT) not in sys.path else None
plugin_module = importlib.import_module("swiggy_instamart.plugin")
SwiggyInstamartPlugin = plugin_module.SwiggyInstamartPlugin


def test_pulse_hindi_script_activation_starts_swiggy_mode():
    registry = InteractionRegistry()
    registry.register("swiggy_instamart", SwiggyInstamartPlugin.INTERACTION_MODES[0])

    streaming = registry.match("heyy हर्मेश order स्विंगी")
    acoustic = registry.match("ऑर्डर्स वेगी.")

    assert streaming is not None
    assert streaming.definition.mode_id == "swiggy_order"
    assert acoustic is not None
    assert acoustic.definition.mode_id == "swiggy_order"


class _FakeSwiggy:
    def __init__(self, *, existing_cart=False):
        self.calls = []
        self.cart = {
            "items": (
                [
                    {
                        "spinId": "old-spin",
                        "skuId": "old-sku",
                        "displayName": "Existing rice",
                        "quantity": 1,
                    }
                ]
                if existing_cart
                else []
            ),
            "cartTotal": 80 if existing_cart else 0,
        }

    async def call(self, server, tool, **arguments):
        self.calls.append((tool, arguments))
        if tool == "get_addresses":
            return SimpleNamespace(
                data={
                    "addresses": [
                        {
                            "addressId": "home-id",
                            "label": "Home",
                            "formattedAddress": "Indiranagar",
                        },
                        {
                            "addressId": "office-id",
                            "label": "Office",
                            "formattedAddress": "Koramangala",
                        },
                    ]
                }
            )
        if tool == "get_cart":
            return SimpleNamespace(data=copy.deepcopy(self.cart))
        if tool == "clear_cart":
            self.cart = {"items": [], "cartTotal": 0}
            return SimpleNamespace(data=copy.deepcopy(self.cart))
        if tool == "search_products":
            return SimpleNamespace(
                data={
                    "products": [
                        {
                            "productId": "milk-product",
                            "displayName": "Toned Milk",
                            "brand": "Amul",
                            "isPromoted": False,
                            "variations": [
                                {
                                    "spinId": "milk-spin",
                                    "skuId": "milk-sku",
                                    "quantityDescription": "1 litre",
                                    "isInStockAndAvailable": True,
                                    "price": {"offerPrice": 62, "mrp": 65},
                                }
                            ],
                        }
                    ]
                }
            )
        if tool == "update_cart":
            names = {"milk-spin": "Toned Milk", "old-spin": "Existing rice"}
            self.cart = {
                "items": [
                    {
                        **value,
                        "displayName": names.get(value["spinId"], "Item"),
                    }
                    for value in arguments["items"]
                ],
                "cartTotal": sum(
                    (62 if value["spinId"] == "milk-spin" else 80) * value["quantity"]
                    for value in arguments["items"]
                ),
            }
            return SimpleNamespace(data=copy.deepcopy(self.cart))
        if tool == "get_payment_options":
            return SimpleNamespace(
                data={
                    "platforms": {
                        "desktop": {
                            "methods": [{"id": "PayWithQR", "label": "Scan QR to pay"}]
                        }
                    },
                    "allMethods": [{"id": "PayWithQR", "label": "Scan QR"}],
                }
            )
        if tool == "checkout":
            return SimpleNamespace(
                data={
                    "status": "PENDING_PAYMENT",
                    "orderId": "order-1",
                    "paasId": "paas-1",
                    "bridgeUrl": "https://payments.example/opaque",
                    "pollingIntervalInMs": 5000,
                    "maxTimeToPollForInMs": 300000,
                }
            )
        raise AssertionError(f"Unexpected fake tool: {tool}")


class _FakeServices:
    def __init__(self):
        self.calls = []

    async def call_plugin(self, plugin_id, action, data, user_id="system"):
        self.calls.append((plugin_id, action, data, user_id))
        return SimpleNamespace(success=True)


def _plugin(fake: _FakeSwiggy, **config_overrides) -> SwiggyInstamartPlugin:
    config = {
        "enabled": True,
        "linked_user_id": "user-1",
        "token_directory": "/private/swiggy",
        "search_samples": 1,
        "review_valid_seconds": 120,
        "checkout_limit_rupees": 1000,
        "llm_operation": "plugin_assistant",
    }
    config.update(config_overrides)
    plugin = SwiggyInstamartPlugin(config)
    plugin.client = fake
    return plugin


def _session(*, phase="starting", state=None, user_id="user-1"):
    now = time.time()
    return InteractionSession(
        interaction_id="interaction-1",
        mode_id="swiggy_order",
        owner_plugin_id="swiggy_instamart",
        user_id=user_id,
        client_id="device-1",
        audio_session_id="audio-1",
        phase=phase,
        plugin_state=state or {},
        started_at=now,
        last_activity_at=now,
        idle_timeout_seconds=600,
        max_duration_seconds=1800,
    )


async def _checkpoint_noop():
    return None


def _context(
    session,
    text,
    *,
    kind="turn",
    services=None,
    checkpoint=_checkpoint_noop,
):
    item = InteractionInput(
        input_id=str(uuid.uuid4()),
        interaction_id=session.interaction_id,
        kind=kind,
        user_id=session.user_id,
        client_id=session.client_id,
        audio_session_id=session.audio_session_id,
        text=text,
        source="streaming",
        received_at=time.time(),
    )
    return InteractionContext(
        session=session,
        input=item,
        services=services,
        checkpoint=checkpoint,
    )


def _apply(session, result):
    if result.phase is not None:
        session.phase = result.phase
    if result.plugin_state is not None:
        session.plugin_state = result.plugin_state


class _ToolChatOperation:
    def __init__(self, completions, name):
        self.model_name = name
        self._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    def get_client(self, is_async=False):
        assert is_async is True
        return self._client

    def to_api_params(self):
        return {"model": self.model_name}

    def prepare_messages(self, messages):
        return messages


class _NeverToolCompletions:
    async def create(self, **_kwargs):
        await asyncio.Event().wait()


class _SearchToolCompletions:
    async def create(self, **_kwargs):
        tool_call = SimpleNamespace(
            function=SimpleNamespace(
                name="search_products",
                arguments='{"query":"paneer"}',
            )
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[tool_call])
                )
            ]
        )


async def _reach_candidates(plugin, services):
    session = _session()
    result = await plugin.on_interaction_start(
        _context(session, "add milk", kind="start", services=services)
    )
    _apply(session, result)
    result = await plugin.on_interaction_turn(
        _context(session, "yes", services=services)
    )
    _apply(session, result)
    return session, result


async def test_start_requires_linked_user_and_never_calls_swiggy():
    fake = _FakeSwiggy()
    plugin = _plugin(fake)

    result = await plugin.on_interaction_start(
        _context(_session(user_id="other-user"), "", kind="start")
    )

    assert result.end
    assert result.end_reason == "unauthorized_user"
    assert fake.calls == []


async def test_start_suggests_only_preferred_current_swiggy_address_tag(monkeypatch):
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    original_call = fake.call

    async def current_address_shape(server, tool, **arguments):
        if tool == "get_addresses":
            fake.calls.append((tool, arguments))
            return SimpleNamespace(
                data={
                    "addresses": [
                        {
                            "id": "home-id",
                            "addressTag": "My home",
                            "addressCategory": "HOME",
                            "addressLine": "Synthetic home address",
                        },
                        {
                            "id": "studio-id",
                            "addressTag": "Studio",
                            "addressCategory": "OTHER",
                            "addressLine": "Synthetic studio address",
                        },
                    ]
                }
            )
        return await original_call(server, tool, **arguments)

    monkeypatch.setattr(fake, "call", current_address_shape)

    result = await plugin.on_interaction_start(_context(_session(), "", kind="start"))

    assert result.phase == "confirm_address"
    assert result.reply == "Use My home for delivery?"
    assert "Studio" not in result.reply
    assert "saved address" not in result.reply.lower()
    assert result.plugin_state["selected_address"] is None
    assert result.plugin_state["addresses"] == [
        {"id": "home-id", "label": "My home"},
        {"id": "studio-id", "label": "Studio"},
    ]


async def test_configured_home_is_preferred_over_first_and_similar_saved_labels(
    monkeypatch,
):
    fake = _FakeSwiggy()
    plugin = _plugin(fake, preferred_address_label="Home")
    original_call = fake.call

    async def addresses_with_similar_labels(server, tool, **arguments):
        if tool == "get_addresses":
            fake.calls.append((tool, arguments))
            return SimpleNamespace(
                data={
                    "addresses": [
                        {"id": "pune-id", "addressTag": "Pune"},
                        {"id": "home-id", "addressTag": "Home"},
                        {"id": "previous-home-id", "addressTag": "Previous Home"},
                        {"id": "tanjul-home-id", "addressTag": "Tanjul Home"},
                    ]
                }
            )
        return await original_call(server, tool, **arguments)

    monkeypatch.setattr(fake, "call", addresses_with_similar_labels)
    session = _session()

    started = await plugin.on_interaction_start(_context(session, "", kind="start"))
    _apply(session, started)

    assert started.reply == "Use Home for delivery?"
    assert started.plugin_state["pending_address"] == {"id": "home-id", "label": "Home"}
    assert started.plugin_state["selected_address"] is None
    assert [name for name, _ in fake.calls] == ["get_addresses"]

    confirmed = await plugin.on_interaction_turn(_context(session, "yes"))

    assert confirmed.plugin_state["selected_address"] == {
        "id": "home-id",
        "label": "Home",
    }
    assert [name for name, _ in fake.calls] == ["get_addresses", "get_cart"]


async def test_spoken_exact_address_label_beats_longer_saved_labels(monkeypatch):
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    original_call = fake.call

    async def addresses_with_similar_labels(server, tool, **arguments):
        if tool == "get_addresses":
            fake.calls.append((tool, arguments))
            return SimpleNamespace(
                data={
                    "addresses": [
                        {"id": "pune-id", "addressTag": "Pune"},
                        {"id": "home-id", "addressTag": "Home"},
                        {"id": "previous-home-id", "addressTag": "Previous Home"},
                        {"id": "tanjul-home-id", "addressTag": "Tanjul Home"},
                    ]
                }
            )
        return await original_call(server, tool, **arguments)

    monkeypatch.setattr(fake, "call", addresses_with_similar_labels)
    session = _session()
    started = await plugin.on_interaction_start(_context(session, "", kind="start"))
    _apply(session, started)
    rejected = await plugin.on_interaction_turn(_context(session, "change address"))
    _apply(session, rejected)

    proposed = await plugin.on_interaction_turn(_context(session, "home"))

    assert proposed.phase == "confirm_address"
    assert proposed.reply == "Use Home for delivery?"
    assert proposed.plugin_state["pending_address"] == {
        "id": "home-id",
        "label": "Home",
    }
    assert proposed.plugin_state["selected_address"] is None
    assert [name for name, _ in fake.calls] == ["get_addresses"]


async def test_preferred_address_waits_for_confirmation_before_cart_lookup():
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    session = _session()

    started = await plugin.on_interaction_start(_context(session, "", kind="start"))
    _apply(session, started)
    unclear = await plugin.on_interaction_turn(_context(session, "continue"))

    assert session.phase == "confirm_address"
    assert unclear.phase == "confirm_address"
    assert [name for name, _ in fake.calls] == ["get_addresses"]

    confirmed = await plugin.on_interaction_turn(_context(session, "yes"))

    assert confirmed.phase == "shopping"
    assert confirmed.plugin_state["selected_address"]["id"] == "home-id"
    assert [name for name, _ in fake.calls] == ["get_addresses", "get_cart"]


async def test_rejecting_suggested_address_confirms_another_saved_label_before_use():
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    session = _session()

    started = await plugin.on_interaction_start(_context(session, "", kind="start"))
    _apply(session, started)
    rejected = await plugin.on_interaction_turn(_context(session, "change address"))
    _apply(session, rejected)
    proposed = await plugin.on_interaction_turn(_context(session, "office"))
    _apply(session, proposed)

    assert rejected.phase == "select_address"
    assert "which saved address" in rejected.reply.lower()
    assert proposed.phase == "confirm_address"
    assert proposed.reply == "Use Office, Koramangala for delivery?"
    assert proposed.plugin_state["selected_address"] is None
    assert [name for name, _ in fake.calls] == ["get_addresses"]

    confirmed = await plugin.on_interaction_turn(_context(session, "yes"))

    assert confirmed.phase == "shopping"
    assert confirmed.plugin_state["selected_address"]["id"] == "office-id"
    assert [name for name, _ in fake.calls] == ["get_addresses", "get_cart"]


async def test_address_choice_precedes_search_and_pending_request_is_resumed():
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    session, result = await _reach_candidates(plugin, _FakeServices())

    assert session.phase == "shopping"
    assert session.plugin_state["selected_address"]["id"] == "home-id"
    assert session.plugin_state["candidates"][0]["spin_id"] == "milk-spin"
    assert "Which number" in result.reply
    tools = [name for name, _ in fake.calls]
    assert tools[0] == "get_addresses"
    assert tools[1] == "get_cart"
    assert tools[2] == "search_products"


async def test_free_form_turn_uses_fast_bounded_agent_and_survives_hung_primary(
    monkeypatch,
):
    fake = _FakeSwiggy()
    plugin = _plugin(fake, llm_timeout_seconds=0.01)
    session = _session(
        phase="shopping",
        state={
            "selected_address": {"id": "home-id", "label": "Home"},
            "candidates": [],
        },
    )
    primary = _ToolChatOperation(_NeverToolCompletions(), "slow-local")
    fallback = _ToolChatOperation(_SearchToolCompletions(), "fast-fallback")
    resolved_default_types = []

    def get_operation(_name, *, default_model_type="llm"):
        resolved_default_types.append(default_model_type)
        return primary

    registry = SimpleNamespace(
        get_llm_operation=get_operation,
        get_fallback_llm_operation=lambda _name, primary, **_kwargs: fallback,
    )
    monkeypatch.setattr(llm_client, "get_models_registry", lambda: registry)

    result = await asyncio.wait_for(
        plugin.on_interaction_turn(_context(session, "could you get paneer")),
        timeout=0.2,
    )

    assert result.phase == "shopping"
    assert result.plugin_state["candidates"][0]["spin_id"] == "milk-spin"
    assert [name for name, _ in fake.calls] == ["search_products"]
    assert resolved_default_types == ["fast_llm"]


async def test_existing_cart_requires_explicit_keep_or_clear():
    fake = _FakeSwiggy(existing_cart=True)
    plugin = _plugin(fake)
    session = _session()
    started = await plugin.on_interaction_start(_context(session, "", kind="start"))
    _apply(session, started)

    confirmed = await plugin.on_interaction_turn(_context(session, "yes"))
    _apply(session, confirmed)
    unclear = await plugin.on_interaction_turn(_context(session, "continue"))

    assert session.phase == "existing_cart_decision"
    assert "keep cart or clear cart" in confirmed.reply.lower()
    assert "still unchanged" in unclear.reply.lower()
    assert not any(name == "clear_cart" for name, _ in fake.calls)


async def test_complete_then_separate_confirm_is_only_checkout_path(monkeypatch):
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    services = _FakeServices()
    session, _ = await _reach_candidates(plugin, services)

    chosen = await plugin.on_interaction_turn(
        _context(session, "first", services=services)
    )
    _apply(session, chosen)
    reviewed = await plugin.on_interaction_turn(
        _context(session, "complete order", services=services)
    )
    _apply(session, reviewed)
    refused = await plugin.on_interaction_turn(
        _context(session, "yes", services=services)
    )

    assert session.phase == "awaiting_confirmation"
    assert "confirm order" in reviewed.reply.lower()
    assert "confirm order" in refused.reply.lower()
    assert not any(name == "checkout" for name, _ in fake.calls)

    monkeypatch.setattr(
        plugin_module,
        "enqueue_instamart_payment_monitor",
        lambda **kwargs: "payment-job-1",
    )
    checkout_checkpoints = []

    async def capture_checkpoint():
        checkout_checkpoints.append(len(fake.calls))

    confirmed = await plugin.on_interaction_turn(
        _context(
            session,
            "confirm order",
            services=services,
            checkpoint=capture_checkpoint,
        )
    )

    checkout_calls = [args for name, args in fake.calls if name == "checkout"]
    checkout_index = [name for name, _ in fake.calls].index("checkout")
    assert checkout_calls == [
        {
            "addressId": "home-id",
            "paymentMethod": "UPI",
            "generateUPIQR": True,
        }
    ]
    assert confirmed.phase == "awaiting_payment"
    assert confirmed.plugin_state["order_id"] == "order-1"
    assert confirmed.event_data["payment_url"].startswith("https://")
    assert services.calls[0][0:2] == ("hermes", "notify")
    assert checkout_checkpoints == [checkout_index]


async def test_agent_can_interpret_natural_completion_as_review_without_checkout(
    monkeypatch,
):
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    services = _FakeServices()
    session, _ = await _reach_candidates(plugin, services)
    _apply(
        session,
        await plugin.on_interaction_turn(_context(session, "first", services=services)),
    )

    async def review_tool_chat(*_args, **_kwargs):
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="review_order", arguments="{}")
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[tool_call])
                )
            ]
        )

    monkeypatch.setattr(plugin_module, "async_chat_with_tools", review_tool_chat)

    reviewed = await plugin.on_interaction_turn(
        _context(session, "that's all for me", services=services)
    )

    assert reviewed.phase == "awaiting_confirmation"
    assert "confirm order" in reviewed.reply.lower()
    assert not any(name == "checkout" for name, _ in fake.calls)


async def test_replayed_cart_checkpoint_reuses_the_exact_full_cart_payload():
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    session, _ = await _reach_candidates(plugin, _FakeServices())
    checkpointed = {}

    async def capture_checkpoint():
        checkpointed["phase"] = session.phase
        checkpointed["state"] = copy.deepcopy(session.plugin_state)

    first = await plugin.on_interaction_turn(
        _context(session, "first", checkpoint=capture_checkpoint)
    )
    assert first.phase == "shopping"
    assert checkpointed["phase"] == "cart_update_in_progress"

    replay_session = _session(phase=checkpointed["phase"], state=checkpointed["state"])
    replayed = await plugin.on_interaction_turn(_context(replay_session, "first"))

    payloads = [args["items"] for name, args in fake.calls if name == "update_cart"]
    assert payloads[0] == payloads[1]
    assert fake.cart["items"][0]["quantity"] == 1
    assert replayed.phase == "shopping"


async def test_changed_cart_forces_another_review_before_checkout():
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    services = _FakeServices()
    session, _ = await _reach_candidates(plugin, services)
    _apply(
        session,
        await plugin.on_interaction_turn(_context(session, "first", services=services)),
    )
    _apply(
        session,
        await plugin.on_interaction_turn(
            _context(session, "complete order", services=services)
        ),
    )
    fake.cart["items"][0]["quantity"] = 2
    fake.cart["cartTotal"] = 124

    result = await plugin.on_interaction_turn(
        _context(session, "confirm order", services=services)
    )

    assert result.phase == "awaiting_confirmation"
    assert "cart changed" in result.reply.lower()
    assert not any(name == "checkout" for name, _ in fake.calls)


async def test_ambiguous_checkout_failure_is_not_retryable(monkeypatch):
    fake = _FakeSwiggy()
    plugin = _plugin(fake)
    services = _FakeServices()
    session, _ = await _reach_candidates(plugin, services)
    _apply(
        session,
        await plugin.on_interaction_turn(_context(session, "first", services=services)),
    )
    _apply(
        session,
        await plugin.on_interaction_turn(
            _context(session, "complete order", services=services)
        ),
    )
    original_call = fake.call

    async def uncertain_checkout(server, tool, **arguments):
        if tool == "checkout":
            fake.calls.append((tool, arguments))
            raise SwiggyError("transport timed out", Bucket.UPSTREAM_TIMEOUT)
        return await original_call(server, tool, **arguments)

    monkeypatch.setattr(fake, "call", uncertain_checkout)
    result = await plugin.on_interaction_turn(
        _context(session, "confirm order", services=services)
    )

    assert result.end
    assert result.end_reason == "checkout_outcome_unknown"
    assert "will not submit it again" in result.reply.lower()
    assert [name for name, _ in fake.calls].count("checkout") == 1


def test_llm_never_receives_checkout_or_confirmation_tools():
    names = {value["function"]["name"] for value in plugin_module._SHOPPING_TOOLS}
    assert "review_order" in names
    assert "checkout" not in names
    assert "confirm_order" not in names
