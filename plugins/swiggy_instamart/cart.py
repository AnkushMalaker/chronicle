"""Pure response-shape and voice-summary helpers for Instamart."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def addresses_from(data: dict | list | None) -> list[dict]:
    if isinstance(data, list):
        return [value for value in data if isinstance(value, dict)]
    if not isinstance(data, dict):
        return []
    values = data.get("addresses") or data.get("savedAddresses") or []
    return [value for value in values if isinstance(value, dict)]


def address_id(address: dict) -> str:
    return str(address.get("addressId") or address.get("id") or "")


def address_label(address: dict) -> str:
    # Current Swiggy MCP responses use addressTag as the user-facing, unique label
    # and addressCategory as its coarser fallback. Prefer those without appending the
    # full addressLine: this value is spoken aloud when the mode starts.
    current_label = address.get("addressTag") or address.get("addressCategory")
    if current_label:
        return str(current_label).strip() or "saved address"

    label = address.get("label") or address.get("type") or address.get("name")
    detail = (
        address.get("formattedAddress")
        or address.get("displayAddress")
        or address.get("address")
        or address.get("addressLine1")
        or address.get("addressLine")
    )
    if isinstance(detail, dict):
        detail = detail.get("formatted") or detail.get("address")
    parts = [str(value).strip() for value in (label, detail) if value]
    return ", ".join(dict.fromkeys(parts)) or "saved address"


def compact_addresses(data: dict | list | None, *, limit: int = 10) -> list[dict]:
    return [
        {"id": address_id(value), "label": address_label(value)}
        for value in addresses_from(data)[:limit]
        if address_id(value)
    ]


def cart_items(cart: dict | list | None) -> list[dict]:
    if isinstance(cart, list):
        return [value for value in cart if isinstance(value, dict)]
    if not isinstance(cart, dict):
        return []
    for key in ("items", "cartItems"):
        values = cart.get(key)
        if isinstance(values, list):
            return [value for value in values if isinstance(value, dict)]
    collected: list[dict] = []
    for key in ("stores", "carts", "storeCarts"):
        groups = cart.get(key)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            values = group.get("items") or group.get("cartItems") or []
            collected.extend(value for value in values if isinstance(value, dict))
    return collected


def item_name(item: dict) -> str:
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    return str(
        item.get("displayName")
        or item.get("name")
        or item.get("productName")
        or product.get("displayName")
        or product.get("name")
        or "item"
    )


def item_quantity(item: dict) -> int:
    raw = item.get("quantity") or item.get("count") or 1
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1


def item_payload(item: dict) -> dict[str, Any] | None:
    variant = item.get("variant") if isinstance(item.get("variant"), dict) else {}
    spin_id = item.get("spinId") or variant.get("spinId")
    if not spin_id:
        return None
    payload: dict[str, Any] = {
        "spinId": str(spin_id),
        "quantity": item_quantity(item),
    }
    sku_id = item.get("skuId") or variant.get("skuId")
    if sku_id:
        payload["skuId"] = str(sku_id)
    return payload


def cart_update_payload(cart: dict | list | None) -> list[dict[str, Any]]:
    result = []
    for item in cart_items(cart):
        payload = item_payload(item)
        if payload and payload["quantity"] > 0:
            result.append(payload)
    return result


def cart_total(cart: dict | list | None) -> float | None:
    if not isinstance(cart, dict):
        return None
    candidates = [
        cart.get("cartTotal"),
        cart.get("total"),
        cart.get("totalAmount"),
        cart.get("payableAmount"),
    ]
    bill = cart.get("billBreakdown") or cart.get("billDetails") or {}
    if isinstance(bill, dict):
        candidates.extend(
            [
                bill.get("total"),
                bill.get("grandTotal"),
                bill.get("toPay"),
                bill.get("payableAmount"),
            ]
        )
    for raw in candidates:
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("amount")
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def cart_fingerprint(cart: dict | list | None, address: str) -> str:
    canonical_items = sorted(
        (
            value.get("spinId", ""),
            value.get("skuId", ""),
            int(value.get("quantity", 0)),
        )
        for value in cart_update_payload(cart)
    )
    canonical = {
        "address": address,
        "items": canonical_items,
        "total": cart_total(cart),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def summarize_cart(cart: dict | list | None, *, limit: int = 3) -> str:
    items = cart_items(cart)
    if not items:
        return "Your cart is empty."
    spoken = [f"{item_quantity(value)} {item_name(value)}" for value in items[:limit]]
    if len(items) > limit:
        spoken.append(f"and {len(items) - limit} more items")
    total = cart_total(cart)
    total_text = f" The total is about {total:g} rupees." if total is not None else ""
    return f"Your cart has {', '.join(spoken)}.{total_text}"


def qr_available(payment_options: dict | list | None) -> bool:
    if not isinstance(payment_options, dict):
        return False
    platforms = payment_options.get("platforms") or {}
    desktop = platforms.get("desktop") if isinstance(platforms, dict) else {}
    methods = desktop.get("methods") if isinstance(desktop, dict) else []
    for method in methods or []:
        if not isinstance(method, dict):
            continue
        text = f"{method.get('id', '')} {method.get('label', '')}".lower()
        if "qr" in text or "scan" in text:
            return True
    return False
