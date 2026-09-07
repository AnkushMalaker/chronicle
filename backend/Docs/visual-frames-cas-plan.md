# Visual Frames + CAS Blob Store — Implementation Plan

> **⚠️ SUPERSEDED — see [`visual-modality-design.md`](visual-modality-design.md)**, which merges this CAS plan with the timeline + Immich + vault-first design and the agreed decisions (segments-as-timeline-events, CAS=screens/Immich=photos, memory-agent promotion). Kept for the CAS internals + wiring detail it carries.

**Status:** Approved design, not yet implemented.
**Goal:** Add images (screenshots from `extras/local-wearable-client`, and ad-hoc uploads) as a first-class modality on the Chronicle timeline, alongside audio. Store the queryable metadata + derived text in MongoDB, and the image *bytes* in a content-addressed store (CAS) on disk. Correlate frames with conversations by **timestamp**, not a hard foreign key. Optionally caption/OCR via Gemma4 and feed the derived text into the existing memory-extraction pipeline.

This document is self-contained: it carries the design rationale plus every exact integration point needed to implement it from a clean context.

---

## 1. Design decisions (settled — do not relitigate)

1. **Image bytes go to CAS on disk, NOT inline in Mongo.** Reason: ~100× the volume and document count of audio, plus large global dedup wins (repeated screens). Inlining millions of ~350 KB JPEGs would bloat the hot DB / working set / backups. Mongo stores only a `content_hash` reference + metadata.
2. **Audio stays inline in Mongo** (unchanged). It is modest-volume (~95 GB/yr), unique (dedup buys nothing), mutable/ordered (split/merge + VAD), and read as a unit. CAS would add a second store with no payoff. The same CAS module can absorb audio later only if volume forces it.
3. **CAS = content-addressed storage**: path derived from the hash of the bytes, sharded by hash prefix to avoid the "millions of files in one dir" problem. Immutable, append-only, dedup-for-free, rsync-able, and placeable on the slow HDD independently of Mongo.
4. **Time is the join key.** A frame is linked to a conversation by `ts ∈ [conv.start, conv.end]`, queried via an index on `(user_id, ts)`. No rigid FK (frames exist without audio and vice-versa; the relation is many-to-many over time). `conversation_id` MAY be denormalized onto the frame as a convenience but is not the source of truth.
5. **Extract vs pre-extracted** is one field (`analysis_status`). The wearable already produces OCR + app/url context → ingest as `extracted`. A bare uploaded image → `pending` → Gemma4 analysis job.
6. **Segment before captioning.** 1 fps all day = millions of frames; never one caption/memory per frame. Group contiguous frames by `(app, time-gap, changed)` and caption the representative changed frame. Deduped (`changed:false`) frames inherit the previous caption.
7. **Memory integration reuses the existing pipeline** — feed `caption + ocr_text` as plain text into `memory_service.add_memory(...)` with the segment id as `source_id`. The memory agent records the derived text into the Markdown vault; bytes never enter the vault.

---

## 2. Storage layout (CAS)

```
<CAS_ROOT>/                     # default ./data/cas ; put on the slow HDD
  blobs/
    a3/
      f9/
        a3f9c2…e1               # filename = full hex content hash, no extension
  tmp/                          # write-then-rename staging (atomic, crash-safe)
```

- **Hash:** `sha256` of the raw bytes (stdlib `hashlib`, no new dependency). `blake3` is faster but adds a dep — defer.
- **Path:** `blobs/<hash[0:2]>/<hash[2:4]>/<hash>`. Two-level fan-out (256×256 dirs) keeps any directory small for millions of objects.
- **Write:** stream to `tmp/<uuid>`, `fsync`, then `os.rename` into final path (atomic). If the final path already exists → it's a dedup hit, discard the tmp file. Never rewrite an existing blob.
- **MIME / extension:** the blob filename has **no extension** (the bytes are addressed by hash; MIME lives in the Mongo doc). The serving endpoint sets `Content-Type` from the doc's `mime`.
- **Deletion / GC:** because dedup means N frame docs may share one blob, **never delete a blob on frame soft-delete.** A separate GC job (Phase 5) deletes blobs whose hash is referenced by zero non-deleted frame docs. v1 can skip GC entirely (blobs are cheap on the HDD).

**CAS module** — new file `backend/src/backend/services/cas/blob_store.py`:

```python
class BlobStore:
    def __init__(self, root: Path): ...
    def put_bytes(self, data: bytes) -> str: ...          # returns content_hash, dedups
    async def put_stream(self, upload) -> tuple[str, int]: # hash + size_bytes while streaming
    def path_for(self, content_hash: str) -> Path: ...
    def exists(self, content_hash: str) -> bool: ...
    def open(self, content_hash: str) -> BinaryIO: ...     # for range/streaming reads
    def delete(self, content_hash: str) -> None: ...       # GC use only
```

Compute the hash while streaming the upload (don't buffer whole files in RAM where avoidable).

---

## 3. Data model

### New: `CaptureFrameDocument`

New file `backend/src/backend/models/capture_frame.py`. Mirror the style of `models/audio_chunk.py` (`AudioChunkDocument`).

```python
from datetime import datetime, timezone
from typing import Optional
from beanie import Document, Indexed
from pydantic import Field

class CaptureFrameDocument(Document):
    # identity / ownership
    frame_id: Indexed(str)                       # uuid4 hex
    user_id: Indexed(str)                         # str(ObjectId)
    client_id: Indexed(str)                       # {user_suffix}-{device}
    source: str                                   # "wearable-screen" | "upload" | "camera"

    # time — THE join key
    ts: datetime                                  # capture time (UTC)

    # blob reference (bytes live in CAS, not here)
    content_hash: Indexed(str)                    # sha256 hex -> CAS path
    mime: str = "image/jpeg"
    width: int
    height: int
    size_bytes: int

    # context captured AT SOURCE (pre-extracted path; from events.jsonl)
    app: Optional[str] = None
    window_title: Optional[str] = None
    url: Optional[str] = None
    ocr_text: Optional[str] = None
    display_index: int = 0
    changed: bool = True                          # false => visually identical to prev frame

    # derived by analysis job (extract path)
    caption: Optional[str] = None
    caption_model: Optional[str] = None
    analysis_status: str = "pending"              # pending|extracted|skipped|failed
    segment_id: Optional[Indexed(str)] = None     # set when grouped into a visual segment

    # correlation (denormalized convenience; not source of truth)
    conversation_id: Optional[Indexed(str)] = None

    # lifecycle
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted: bool = False
    deleted_at: Optional[datetime] = None

    class Settings:
        name = "capture_frames"
        indexes = [
            [("user_id", 1), ("ts", 1)],          # timeline + "frames during conversation X"
            "content_hash",
            "conversation_id",
            "segment_id",
            "deleted",
        ]
```

Notes:
- **No `audio_data`-style inline bytes.** This is the deliberate difference from `AudioChunkDocument`.
- Ingest with `analysis_status="extracted"` when `ocr_text`/context are supplied; `"pending"` for a bare image that should be captioned.

### `Conversation` — no required schema change for v1

Correlation is a query (`user_id` + `ts` range). Optionally (Phase 4+) cache `frame_count` / `has_visual` on the conversation for UI badges, but not required to ship.

---

## 4. Ingestion endpoints

New router module `backend/src/backend/routers/modules/frame_routes.py`:

```python
router = APIRouter(prefix="/frames", tags=["frames"])   # served at /api/frames
```

Endpoints:

1. **`POST /api/frames`** — single image, ad-hoc context.
   - Multipart: `file` (image), optional `ts` (defaults to now), optional `app`/`url`/`ocr_text`, `extract: bool = false`, optional `device_name`.
   - Stream bytes → `BlobStore.put_bytes` → `content_hash`, read width/height (Pillow), create `CaptureFrameDocument`.
   - If `extract=true` and no `ocr_text`/`caption` supplied → `analysis_status="pending"` and enqueue `frame_analysis_job`. Else `"extracted"`.
   - Auth: `Depends(current_active_user)`. `client_id = generate client id from user + device_name`.

2. **`POST /api/frames/batch`** — wearable sidecar sync (the primary path).
   - Accepts the `events.jsonl` content + the referenced JPEG files (multipart list, or a `.tar`/`.zip`). The wearable's `events.jsonl` schema (per tick): `ts`, `epoch`, `app`, `bundle_id`, `window_title`, `url`, `focused_role`, `idle_seconds`, `screenshots` (status), and `displays[]` each with `{index, file, w, h, ocr_file, changed}`.
   - For each `displays[]` entry with `changed:true` and a present file: put blob, read OCR from the referenced `.txt` if shipped, create frame doc with `analysis_status="extracted"` (context is pre-extracted), `changed` flag carried through.
   - For `changed:false` entries: still create a frame doc, but point `content_hash` at the previous blob (CAS dedups automatically since bytes are identical) and inherit the previous caption later. Skip captioning.
   - Idempotency: dedup on `(user_id, ts, display_index, content_hash)` so re-running a sync doesn't double-insert.
   - Auth: `Depends(current_active_user)` (or `current_superuser` if you want to mirror `/api/audio/upload`, which is admin-only at `audio_routes.py:58`).

---

## 5. Retrieval endpoints

1. **`GET /api/frames/{frame_id}/image`** — stream bytes from CAS.
   - Look up doc → `BlobStore.open(content_hash)` → `StreamingResponse` with `media_type=doc.mime`. Support HTTP Range (mirror the range handling in `audio_routes.py` `get_conversation_audio`). Set `Cache-Control: immutable` (content-addressed → safe).
2. **`GET /api/frames`** — metadata list for a time window.
   - Query params: `from`, `to` (ISO), `conversation_id` (resolve to its time window), `client_id`, `changed_only`, pagination.
   - Powers the timeline / filmstrip UI: on a conversation detail page, fetch frames where `user_id == me AND ts ∈ [conv.start, conv.end]` → render thumbnails "at the same time as the recording."
3. (Optional) **`GET /api/frames/{frame_id}`** — single frame metadata (caption, ocr, context).

---

## 6. Analysis job (Gemma4) — the "extract" path

New file `backend/src/backend/workers/frame_jobs.py`.

```python
@async_job(redis=True, beanie=True)
@traced_job("frame_analysis", pipeline_stage="frame_analysis", gen_ai_operation="chat")
async def frame_analysis_job(frame_id: str, *, redis_client=None) -> dict:
    # imports INSIDE the function (worker convention)
    from backend.models.capture_frame import CaptureFrameDocument
    # ... load frame; if deleted/extracted -> return {"success": True, "skipped": True}
    # ... read bytes from CAS, call Gemma4 vision (the gemma4-asr/vision service)
    #     for a caption (+ OCR if ocr_text missing)
    # ... set caption/caption_model/analysis_status="extracted"; save
    # RETURN A DICT on failure; never raise (raising defers dependent jobs forever)
    return {"success": True, "frame_id": frame_id}
```

Enqueue helper (model on `enqueue_memory_processing`, `workers/memory_jobs.py:521`):

```python
def enqueue_frame_analysis(frame_id: str, depends_on=None):
    return default_queue.enqueue(
        frame_analysis_job, frame_id,
        job_timeout=300, result_ttl=JOB_RESULT_TTL,
        job_id=f"frame_{frame_id[:12]}", depends_on=depends_on,
        meta={"frame_id": frame_id},
    )
```

- **Reuse the `default` (or `memory`) queue** — the 6 `rq-worker-*` workers already serve `transcription`/`memory`/`default` (`workers/orchestrator/worker_registry.py:133-149`). **No worker-registration change needed** unless you want a dedicated queue.
- **Gemma4 access:** call the existing vision service over HTTP (the gemma4-asr/vision service the project already runs). Do not load models in the backend image (project rule: heavy deps in a separate `extras/` service called over HTTP).

---

## 7. Segmentation + memory integration

Phase 4. Implement as a function over frames (no new collection required in v1):

- **Grouping:** sort a user's frames by `ts`; start a new segment when `app` changes OR the time gap exceeds a threshold (e.g. 30–60 s) OR a configurable max segment length is hit. Assign all frames in a group a shared `segment_id` (deterministic: e.g. `f"{client_id}:{first_frame_epoch}"`).
- **Caption the representative changed frame** of the segment; deduped frames inherit.
- **Feed text into memory** — do NOT call `enqueue_memory_processing` (it fetches a `Conversation` doc by id). Instead call the provider directly:

```python
from backend.services.memory import get_memory_service
memory_service = get_memory_service()
visual_text = build_segment_text(segment)   # caption + OCR + app/url context
await memory_service.add_memory(
    visual_text, client_id, segment_id,      # segment_id is the source_id
    user_id, user_email, allow_update=True,
)
```

`add_memory` signature (`services/memory/base.py:95`): `add_memory(transcript, client_id, source_id, user_id, user_email, allow_update=False, db_helper=None)`. There is **no `source_type`** param — the source is just the opaque `source_id` + text. (If provenance distinction matters later, prefix the `source_id`, e.g. `visual:{segment_id}`.)

---

## 8. Exact integration points (wiring map)

All paths under `backend/src/backend/`.

| What | File / location | Action |
|---|---|---|
| **Register Beanie model** | `app_factory.py:139-156` (`init_beanie(... document_models=[...])`) | Import `CaptureFrameDocument`, add to the list (after `MemoryAuditEntry`). |
| Other `init_beanie` sites | `cron.py:59`, `models/job.py:66`, `scripts/cleanup_state.py:885` | Add `CaptureFrameDocument` only if a worker/cron path touches frames. The `@async_job(beanie=True)` decorator (`models/job.py:32/279`) initializes Beanie in workers — **check whether it uses the `app_factory` list or its own**; if its own, frames must be added there for `frame_analysis_job` to query them. **Verify before implementing.** |
| **New router** | `routers/modules/frame_routes.py` (new) | `router = APIRouter(prefix="/frames", tags=["frames"])` |
| Export router | `routers/modules/__init__.py:24,45` | `from .frame_routes import router as frame_router` + add to `__all__`. |
| Mount router | `routers/api_router.py:13-31` (imports) + `:41-43` (includes) | `router.include_router(frame_router)`. Auto-served at `/api/frames`. No `app_factory.py` change needed. |
| **CAS data dir** | `app_config.py:45-46` (`__init__`) + `:102-104` (getter) | Add `self.cas_blob_dir = Path(os.getenv("CAS_BLOB_DIR", "./data/cas")); mkdir(...)` and `def get_cas_blob_dir() -> Path`. |
| **Volume mount** | `backend/docker-compose.yml` (backend + worker services) | Mount the CAS dir on a host volume (the slow HDD). Confirm both the backend and the `worker_orchestrator` (`docker-compose.yml:79`) containers see the same path. Note `app_factory.py:584` hardcodes `/app/audio_chunks` for static audio mount — CAS is served via endpoint (streamed), so no static mount, but the container path must match `CAS_BLOB_DIR`. |
| **Auth deps** | `auth.py:166-168` | `current_active_user` (normal) / `current_superuser` (admin). Import from `backend.auth`. |
| **RQ queues** | `controllers/queue_controller.py:78-101` | Reuse `default_queue` (or `memory_queue`). Add a `frames` queue only if you want isolation (then update `worker_registry.py:135-148` queue args). |
| **Job conventions** | `workers/memory_jobs.py:123-129` (decorators), `models/job.py:227` (`@async_job`) | `@async_job(redis=True, beanie=True)`, async, imports inside fn, **return dict / never raise**. |
| **Enqueue pattern** | `workers/memory_jobs.py:521-559` | Model `enqueue_frame_analysis` on `enqueue_memory_processing`. |
| **Memory provider** | `services/memory/base.py:95`, invoked `workers/memory_jobs.py:270-277` | Call `memory_service.add_memory(text, client_id, source_id, user_id, user_email, allow_update=True)` directly. |
| **Audio model to mirror** | `models/audio_chunk.py` | Style reference for the new model + soft-delete/archival fields. |
| **Range-serving to mirror** | `routers/modules/audio_routes.py` (`get_conversation_audio`, ~:71) | Copy the Range-request handling for `GET /api/frames/{id}/image`. |

---

## 9. Phased implementation + checklist

**Phase 1 — CAS + model + single-image flow (ship first, makes bytes viewable):**
- [ ] `services/cas/blob_store.py` (`BlobStore`, sha256, sharded paths, atomic write, dedup).
- [ ] `app_config.py`: `cas_blob_dir` + `get_cas_blob_dir()`.
- [ ] `docker-compose.yml`: volume mount for CAS dir on backend (+ worker) containers.
- [ ] `models/capture_frame.py`: `CaptureFrameDocument`.
- [ ] Register model in `app_factory.py` `init_beanie` list.
- [ ] `routers/modules/frame_routes.py`: `POST /api/frames` (pre-extracted path) + `GET /api/frames/{id}/image` (range) + `GET /api/frames` (time window).
- [ ] Export + mount router (`modules/__init__.py`, `api_router.py`).
- [ ] Smoke test: upload a JPEG, fetch it back, confirm dedup (upload same bytes twice → one blob).

**Phase 2 — wearable batch ingestion:**
- [ ] `POST /api/frames/batch` (events.jsonl + JPEGs, idempotent, `changed` handling).
- [ ] Frame-uploader in `extras/local-wearable-client` (walk per-day folders + `events.jsonl`, push changed frames + OCR/context; reuse the JWT auth from `backend_sender.py`). It currently uploads **audio only** — this is net-new.

**Phase 3 — analysis job (extract path):**
- [ ] `workers/frame_jobs.py`: `frame_analysis_job` + `enqueue_frame_analysis` (Gemma4 vision over HTTP).
- [ ] Wire `POST /api/frames` `extract=true` → enqueue.
- [ ] Verify worker Beanie-init includes the new model (see table caveat).

**Phase 4 — segmentation + memory:**
- [ ] Segment grouping function `(app, time-gap, changed)`.
- [ ] Build segment text, call `memory_service.add_memory(... source_id=segment_id ...)`.
- [ ] (Optional) cache `frame_count`/`has_visual` on `Conversation`; add timeline/filmstrip to the conversation detail page in `webui/`.

**Phase 5 — lifecycle:**
- [ ] Blob GC job (delete blobs with zero referencing non-deleted frame docs).
- [ ] Retention policy (mirror wearable's: optional age-out of cold frames).

---

## 10. Open decisions (deferred — pick when reached, not blocking)

- **Ingestion cadence:** batch sidecar sync (Phase 2, recommended start) vs. live frame push over WS. Start batch.
- **Blob backend:** local CAS dir now; the `BlobStore` interface abstracts cleanly to S3/MinIO later.
- **v1 search:** text-only (caption+OCR → memory agent → Markdown vault, reuses everything). CLIP image embeddings deferred.
- **Hash:** sha256 (stdlib) for v1; blake3 only if hashing becomes a bottleneck.

---

## 11. Testing

- Follow `tests/TESTING_GUIDELINES.md` and `tests/tags.md` (only approved tags) if adding Robot tests. Likely tag: `infra` (or a new component tag only with approval — do not invent tags).
- Unit-test `BlobStore` (dedup, atomic write, sharded path, missing-blob behavior) without Mongo.
- Endpoint tests can run under `make test-no-api` (no API keys) for upload/retrieve/list; the Gemma4 analysis path needs the vision service and should be gated like `requires-api-keys`/GPU.
