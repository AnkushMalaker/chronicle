"""HTTP client for the intent-router microservice.

Keeps the heavy ML deps (model2vec, scikit-learn) out of the backend image: the
classifier runs in the separate `intent-router` service and we call it over a
fast localhost/compose-network HTTP hop (~1-3ms on top of ~0.14ms inference).

Fail-open policy: if the service is unreachable, default to route='home' so the
command still enters the HA cascade (which itself falls back to Hermes). A router
outage therefore degrades latency, never correctness.
"""

import logging
import os
import time

import httpx

from .cascade import RouteInfo

logger = logging.getLogger(__name__)

INTENT_ROUTER_URL = os.getenv("INTENT_ROUTER_URL", "http://intent-router:8791")
_TIMEOUT = float(os.getenv("INTENT_ROUTER_TIMEOUT", "2.0"))

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def classify(text: str) -> RouteInfo:
    t0 = time.time()
    try:
        resp = await _get_client().post(
            f"{INTENT_ROUTER_URL}/classify", json={"text": text}
        )
        resp.raise_for_status()
        d = resp.json()
        return RouteInfo(
            route=d["route"], p_home=d["p_home"], latency_ms=d.get("latency_ms", 0.0)
        )
    except Exception as e:
        logger.warning("intent-router unreachable (%s); failing open to 'home'", e)
        return RouteInfo(route="home", p_home=1.0, latency_ms=(time.time() - t0) * 1000)
