"""ColPali-family visual retrieval service.

Embeds screenshots as patch vectors and answers text queries over them, so an image
can be found by what it looks like rather than only by the words some other pass
wrote about it.

The model is loaded lazily and unloaded again after an idle period. This runs on a
desktop GPU that is also driving a display, where free VRAM is a moving target and
embedding is a trickle workload — a handful of images a day. A cold start of ten or
twenty seconds costs nothing because nothing is waiting on it: the backend queues
work and reconciles later.
"""

import argparse
import asyncio
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from index import VisualIndex
from PIL import Image
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("colpali-service")

MODEL_ID = os.getenv("COLPALI_MODEL", "vidore/colSmol-256M")
INDEX_DIR = Path(os.getenv("COLPALI_INDEX_DIR", "/index"))
IDLE_UNLOAD_SECONDS = int(os.getenv("COLPALI_IDLE_UNLOAD_SECONDS", "900"))
MAX_IMAGE_BYTES = int(os.getenv("COLPALI_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
# Long edge the image is scaled to before embedding. ColPali-family processors do
# their own resizing; capping here just avoids decoding a huge image needlessly.
MAX_EDGE = 1536

app = FastAPI(title="Chronicle ColPali Service", version="1.0.0")
index = VisualIndex(INDEX_DIR)


class _Model:
    """Lazily loaded model, unloaded again once idle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self._last_used = 0.0
        # `or`, not a getenv default: compose passes COLPALI_DEVICE through as an
        # empty string when it is unset, which is "present" as far as getenv is
        # concerned and would otherwise silently become the device name.
        self.device = os.getenv("COLPALI_DEVICE") or (
            "cuda" if torch.cuda.is_available() else "cpu"
        )

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load_locked(self) -> None:
        if self._model is not None:
            return
        # Imported at use because loading colpali_engine pulls in the whole
        # transformers stack, which we do not want on a health-check-only process.
        from colpali_engine.models import (
            ColIdefics3,
            ColIdefics3Processor,
            ColQwen2,
            ColQwen2Processor,
        )

        lowered = MODEL_ID.lower()
        if "qwen" in lowered:
            model_cls, processor_cls = ColQwen2, ColQwen2Processor
        else:
            model_cls, processor_cls = ColIdefics3, ColIdefics3Processor
        logger.info("Loading %s onto %s", MODEL_ID, self.device)
        dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32
        self._model = model_cls.from_pretrained(
            MODEL_ID, torch_dtype=dtype, device_map=self.device
        ).eval()
        self._processor = processor_cls.from_pretrained(MODEL_ID)
        logger.info("Loaded %s", MODEL_ID)

    def acquire(self):
        with self._lock:
            self._load_locked()
            self._last_used = time.time()
            return self._model, self._processor

    def maybe_unload(self) -> bool:
        with self._lock:
            if self._model is None or IDLE_UNLOAD_SECONDS <= 0:
                return False
            if time.time() - self._last_used < IDLE_UNLOAD_SECONDS:
                return False
            logger.info("Unloading %s after %ss idle", MODEL_ID, IDLE_UNLOAD_SECONDS)
            self._model = None
            self._processor = None
            if self.device.startswith("cuda"):
                torch.cuda.empty_cache()
            return True


model = _Model()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    user_id: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)


def _embed_images(images: list[Image.Image]) -> np.ndarray:
    net, processor = model.acquire()
    batch = processor.process_images(images).to(net.device)
    with torch.no_grad():
        out = net(**batch)
    return out.to(torch.float32).cpu().numpy()[0]


def _embed_query(text: str) -> np.ndarray:
    net, processor = model.acquire()
    batch = processor.process_queries([text]).to(net.device)
    with torch.no_grad():
        out = net(**batch)
    return out.to(torch.float32).cpu().numpy()[0]


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness for discovery. Deliberately does not touch the GPU or load a model."""
    return {
        "status": "healthy",
        "model": MODEL_ID,
        "device": model.device,
        "loaded": model.loaded,
        **index.stats(),
    }


@app.get("/info")
async def info() -> dict[str, Any]:
    return {
        "service": "chronicle-colpali",
        "provider": "colpali",
        "model": MODEL_ID,
        "capabilities": ["image_embedding", "text_to_image_search"],
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
    }


@app.post("/embed")
async def embed(
    file: UploadFile = File(...),
    doc_id: str = Form(...),
    user_id: str = Form(...),
    metadata: str = Form(default="{}"),
) -> dict[str, Any]:
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the size limit")
    try:
        meta = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Malformed metadata: {exc}")
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:  # Pillow raises many types for a bad image.
        raise HTTPException(status_code=415, detail=f"Unreadable image: {exc}")
    image.thumbnail((MAX_EDGE, MAX_EDGE))

    vectors = await asyncio.to_thread(_embed_images, [image])
    patches = await asyncio.to_thread(
        index.add, user_id, doc_id, vectors, meta, MODEL_ID
    )
    return {
        "doc_id": doc_id,
        "patches": patches,
        "dim": int(vectors.shape[1]),
        "model": MODEL_ID,
    }


@app.post("/search")
async def search(request: SearchRequest) -> dict[str, Any]:
    query = await asyncio.to_thread(_embed_query, request.query)
    hits = await asyncio.to_thread(
        index.search, request.user_id, query, request.limit, MODEL_ID
    )
    return {"model": MODEL_ID, "hits": hits}


@app.get("/documents")
async def documents(user_id: str) -> dict[str, Any]:
    """Doc ids indexed *by the currently loaded model*.

    Reporting only same-model documents is what lets the backend self-heal after a
    model change or a wiped index: anything it believes is embedded but is missing
    here simply gets embedded again.
    """
    return {"model": MODEL_ID, "doc_ids": index.documents(user_id, model=MODEL_ID)}


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, user_id: str) -> dict[str, Any]:
    return {"deleted": index.remove(user_id, doc_id)}


@app.on_event("startup")
async def _start_idle_reaper() -> None:
    async def reaper() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(model.maybe_unload)
            except Exception:
                logger.exception("Idle unload failed")

    asyncio.create_task(reaper())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8790")))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
