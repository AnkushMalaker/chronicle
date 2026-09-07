"""Latency contract for Instamart's sample-and-union search helper."""

import asyncio
from types import SimpleNamespace

from backend.integrations.swiggy import search_products


class _ConcurrentOnlyClient:
    def __init__(self, expected_calls: int):
        self.expected_calls = expected_calls
        self.started = 0
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()

    async def call(self, server, tool, **arguments):
        assert tool == "search_products"
        self.started += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.started == self.expected_calls:
            self.release.set()
        try:
            await asyncio.wait_for(self.release.wait(), timeout=0.05)
            return SimpleNamespace(data={"products": []})
        finally:
            self.active -= 1


class _StaticClient:
    async def call(self, server, tool, **arguments):
        return SimpleNamespace(
            data={
                "products": [
                    {
                        "productId": "sponsored",
                        "displayName": "Sponsored Banana",
                        "isPromoted": True,
                    },
                    {
                        "productId": "banana",
                        "displayName": "Baby Banana",
                        "isPromoted": False,
                    },
                    {
                        "productId": "apple-ber",
                        "displayName": "Apple Ber",
                        "isPromoted": False,
                    },
                ]
            }
        )


async def test_search_samples_start_concurrently():
    client = _ConcurrentOnlyClient(expected_calls=3)

    result = await search_products(client, "address-1", "bananas", samples=3)

    assert result.samples == 3
    assert client.max_active == 3


async def test_search_preserves_provider_relevance_within_organic_results():
    result = await search_products(_StaticClient(), "address-1", "bananas", samples=1)

    assert [product.name for product in result.products] == [
        "Baby Banana",
        "Apple Ber",
        "Sponsored Banana",
    ]
