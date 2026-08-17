"""Sample-and-union Instamart search results from its varying backend."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Iterable

from .client import Server, SwiggyClient

DEFAULT_SAMPLES = 3


@dataclass
class Variant:
    spin_id: str
    sku_id: str
    label: str
    quantity: str
    price: float
    mrp: float
    in_stock: bool


@dataclass
class Product:
    product_id: str
    name: str
    brand: str
    promoted: bool
    variants: list[Variant] = field(default_factory=list)

    @property
    def cheapest(self) -> Variant | None:
        available = [variant for variant in self.variants if variant.in_stock]
        return min(
            available or self.variants, key=lambda value: value.price, default=None
        )


@dataclass
class SearchResult:
    products: list[Product]
    samples: int
    sizes: list[int]
    queries: list[str]

    @property
    def organic(self) -> list[Product]:
        return [product for product in self.products if not product.promoted]


def _variant(raw: dict) -> Variant:
    price = raw.get("price") or {}
    return Variant(
        spin_id=raw.get("spinId", ""),
        sku_id=raw.get("skuId", ""),
        label=raw.get("displayName", ""),
        quantity=raw.get("quantityDescription", ""),
        price=float(price.get("offerPrice", 0) or 0),
        mrp=float(price.get("mrp", 0) or 0),
        in_stock=bool(raw.get("isInStockAndAvailable", False)),
    )


def _product(raw: dict) -> Product:
    return Product(
        product_id=raw.get("productId", ""),
        name=raw.get("displayName", ""),
        brand=raw.get("brand", ""),
        promoted=bool(raw.get("isPromoted", False)),
        variants=[_variant(value) for value in raw.get("variations", [])],
    )


async def search_products(
    client: SwiggyClient,
    address_id: str,
    query: str,
    *,
    samples: int = DEFAULT_SAMPLES,
    also: Iterable[str] = (),
) -> SearchResult:
    if samples < 1:
        raise ValueError("samples must be at least 1")
    queries = [query, *also]
    responses = await asyncio.gather(
        *(
            client.call(
                Server.INSTAMART,
                "search_products",
                addressId=address_id,
                query=value,
            )
            for value in queries
            for _ in range(samples)
        )
    )

    merged: dict[str, Product] = {}
    sizes: list[int] = []
    for response in responses:
        raw = response.data if isinstance(response.data, dict) else {}
        page = raw.get("products") or []
        sizes.append(len(page))
        for item in page:
            product = _product(item)
            if not product.product_id:
                continue
            existing = merged.get(product.product_id)
            if existing is None or (existing.promoted and not product.promoted):
                merged[product.product_id] = product
    # Python's sort is stable: this moves promotions behind organic results while
    # retaining the provider's relevance order within each group.
    ordered = sorted(merged.values(), key=lambda value: value.promoted)
    return SearchResult(ordered, samples, sizes, queries)
