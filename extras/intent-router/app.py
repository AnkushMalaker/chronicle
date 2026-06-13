"""Intent-router microservice.

A tiny, dependency-light service that classifies a short voice command as a
home-automation request ("home") vs a general agent/chat query ("other"),
using a sub-millisecond local Model2Vec static embedding + logistic-regression
head. Kept separate from the backend so the heavy-ish ML deps (model2vec,
scikit-learn) don't bloat the main image.

Endpoints:
    GET  /health           -> {status, loaded, model}
    POST /classify {text}  -> {route, p_home, label, latency_ms, ok}
"""

import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel
from router import get_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("intent-router")

app = FastAPI(title="Intent Router", version="1.0")


class ClassifyRequest(BaseModel):
    text: str


class ClassifyResponse(BaseModel):
    route: str
    p_home: float
    label: str
    latency_ms: float
    ok: bool


@app.on_event("startup")
async def _startup():
    # Load model + classifier and run one inference so the first request is fast.
    get_router().warm()
    logger.info("Intent router warmed and ready")


@app.get("/health")
async def health():
    r = get_router()
    loaded = r._ensure_loaded()
    return {
        "status": "healthy" if loaded else "degraded",
        "loaded": loaded,
        "model": r._classes,
        "threshold": r.threshold,
    }


@app.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest):
    res = get_router().classify(req.text)
    return ClassifyResponse(
        route=res.route,
        p_home=res.p_home,
        label=res.label,
        latency_ms=res.latency_ms,
        ok=res.ok,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("INTENT_ROUTER_PORT", "8791")))
