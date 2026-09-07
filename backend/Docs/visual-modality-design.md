# Visual Modality Design — CAS + Immich + Vault

**Status:** Approved design, not yet implemented. **This is the authoritative doc.** It supersedes and folds in `/timeline_transition_plan.md` (which predates the vault-first pivot) and the earlier `visual-frames-cas-plan.md`.

**One-line:** Images become first-class timeline events with two byte-backends (CAS for the screenshot firehose, Immich for photos), and a curated subset is *promoted* by the memory agent into the Obsidian vault as embedded `![[ ]]` images. The Markdown vault is the single memory store (FalkorDB and all other memory indexes have been removed).

---

## 1. The mental model: two planes

The single most important idea. Images live on **two planes**, and most images never leave the first:

```
CONTEXT PLANE  (the timeline)              MEMORY PLANE  (the vault)
─────────────────────────────             ──────────────────────────────
every image, indexed by time              curated, connected notes
backends: CAS (screenshots)               system of record = Obsidian vault
        + Immich (photos)                 ![[embedded]] images + [[wikilinks]]
queryable, retention-bounded              vault = the sole memory store
NOT synced to Obsidian                    synced to all devices via Syncthing
"what was happening"                      "what's worth remembering"

            └──────── PROMOTION (memory agent decides) ────────┘
        most images stay context-only and never enter the vault
```

- **Context plane** = the timeline. Conversations and images are **peer events** correlated by time (not parent/child). This is the `timeline_transition_plan.md` territory, still valid.
- **Memory plane** = the Obsidian vault. The `MemoryAgent` writes structured notes; the vault is the single source of truth (no separate index). Images that earn a place are embedded into notes.
- **Promotion** is the bridge: the memory agent's existing judgment, extended to images. Not a separate subsystem.

---

## 2. Settled decisions (do not relitigate)

1. **Two byte-stores, one model.** Wearable screenshots → **CAS** on disk (backend-only, never synced). Camera photos & photo-uploads → **Immich** (reference only). One `ImageEvent` model with a `storage: "cas" | "immich"` discriminator. *(decision: "CAS=screens, Immich=photos")*
2. **Segments, not raw frames, are timeline events.** The ~86k screenshots/day live in CAS with lightweight `capture_frames` records. Only **segments** (contiguous frames grouped by app + time gap) and standalone photos become timeline `ImageEvent`s. Keeps Mongo lean (~hundreds of events/day, not 86k). *(decision: "Segments, not raw frames")*
3. **The memory agent decides promotion.** The vault LLM agent judges memory-worthiness and embeds only meaningful images (whiteboards, documents, people, places). Most screenshots → no note. *(decision: "Memory agent decides")*
4. **Audio stays inline in Mongo** (unchanged) — modest volume, unique (no dedup win), mutable/ordered. CAS is for the image firehose.
5. **CAS = content-addressed**: sha256, sharded path, immutable, dedup-for-free, placeable on the slow HDD.
6. **Time is the join key.** "Photos near this conversation" = a `captured_at` range query, not a stored FK.
7. **Memory = vault only.** Visual memory flows through `add_memory(...)` → `MemoryAgent` → vault notes; the vault is the single store.

### Reconciliation fixes vs the old `timeline_transition_plan.md`
- **Drop `memory_versions` on `ImageEvent`** — memory-versioning was removed in favor of the `memory_audit` ledger. ImageEvent uses the ledger like conversations.
- **No vector/graph index at all** — the vault is the sole memory store (FalkorDB and Qdrant are both gone). Reword any "Immich is the backend the way Qdrant is the vector backend."
- The old plan's memory section fed FalkorDB as primary; here it feeds the **vault agent** which owns promotion + embedding into the vault.

---

## 3. Data model

Two collections. All paths under `backend/src/backend/`.

### 3a. `capture_frames` — the raw screenshot firehose (lightweight, CAS-backed, prunable)

New `models/capture_frame.py`. One tiny doc per *changed* wearable frame. Not a timeline event; the substrate segments are built from.

```python
class CaptureFrameDocument(Document):
    frame_id: Indexed(str)                 # uuid
    user_id: Indexed(str)
    client_id: Indexed(str)                # {user_suffix}-{device}
    ts: datetime                           # capture time (UTC)
    content_hash: Indexed(str)             # sha256 -> CAS blob (bytes NOT stored here)
    mime: str = "image/jpeg"
    width: int; height: int; size_bytes: int
    display_index: int = 0
    changed: bool = True                   # false => identical to prev (dedup; inherit)
    # context captured at source (from wearable events.jsonl)
    app: Optional[str] = None
    window_title: Optional[str] = None
    url: Optional[str] = None
    ocr_text: Optional[str] = None
    segment_id: Optional[Indexed(str)] = None   # set once grouped
    created_at: datetime; deleted: bool = False; deleted_at: Optional[datetime] = None
    class Settings:
        name = "capture_frames"
        indexes = [[("user_id", 1), ("ts", 1)], "content_hash", "segment_id", "deleted"]
```
~200-byte docs; ~31M/yr at full firehose ≈ ~6 GB/yr in Mongo (metadata only). **Prunable** — once a segment is built (and any image promoted), raw frames can be aged out aggressively; the segment + CAS blobs persist.

### 3b. `image_events` — timeline peers (segments + photos)

New `models/image_event.py`. This is the **timeline event** and the **memory candidate**. Holds: (a) screenshot *segments*, (b) individual camera photos, (c) individual uploads.

```python
class ImageEvent(Document):
    event_id: Indexed(str)                 # uuid
    user_id: Indexed(str)
    captured_at: datetime                  # absolute; segment start, or photo EXIF time
    kind: str                              # "screen_segment" | "photo"
    source: str                            # "wearable-screen" | "immich_sync" | "upload" | "camera"

    # --- storage discriminator ---
    storage: str                           # "cas" | "immich"
    content_hash: Optional[str]            # sha256 (dedup, both stores)
    cas_hash: Optional[str]                # representative frame blob (storage=cas)
    immich_asset_id: Optional[str]         # (storage=immich)
    immich_thumbnail_url: Optional[str]

    # --- segment span (kind=screen_segment) ---
    segment_id: Optional[Indexed(str)]
    span_start: Optional[datetime]; span_end: Optional[datetime]
    frame_count: Optional[int]
    app: Optional[str]                     # dominant app for the segment

    # --- derived / enrichment ---
    caption: Optional[str]                 # Gemma4 (screens) or Immich/vision (photos)
    description: Optional[str]
    detected_objects: list[str] = []
    detected_text: Optional[str]           # OCR (screen: aggregated; photo: Immich/vision)
    detected_people: list[str] = []        # Immich face names -> become [[Person]] links
    location: Optional[dict] = None        # {lat,lng,city,country} (photo EXIF/Immich)
    tags: list[str] = []

    # --- pipeline + promotion ---
    processing_status: str = "pending"     # pending|processing|completed|failed|skipped
    promoted: bool = False                 # crossed into the vault?
    vault_note_path: Optional[str]         # e.g. "Conversations/<id>.md" or "Topics/<x>.md"
    vault_media_path: Optional[str]        # "_media/<hash>.jpg" if embedded

    import_batch_id: Optional[Indexed(str)] = None
    created_at: datetime; deleted: bool = False; deleted_at: Optional[datetime] = None
    class Settings:
        name = "image_events"
        indexes = [
            [("user_id", 1), ("captured_at", -1)],   # timeline + photos-near-conversation
            "event_id", "content_hash", "immich_asset_id", "segment_id",
            "import_batch_id", "promoted", "deleted",
        ]
```

`Conversation` needs **no required change**; correlation is the `captured_at` range query. (Optional later: cache `image_event_count` for UI badges.)

---

## 4. Storage substrate

### 4a. CAS (screenshots) — `services/cas/blob_store.py` (new)

```
<CAS_ROOT>/  (default ./data/cas ; put on the slow HDD; NEVER under conversation_docs)
  blobs/<h[0:2]>/<h[2:4]>/<sha256>     # no extension; MIME in the Mongo doc
  tmp/                                  # write -> fsync -> os.rename (atomic, crash-safe)
```
- sha256 (stdlib). `put_bytes(data)->hash` dedups (skip if path exists). `open(hash)`, `path_for(hash)`, `exists(hash)`, `delete(hash)` (GC only).
- **Never delete a blob on frame soft-delete** (N frames share a blob). GC job (late phase) removes blobs with zero live referents. v1 can skip GC.
- Served via endpoint (range-capable), **not** a static mount.

### 4b. Immich (photos) — `services/immich/client.py` (new, from old plan)

```python
class ImmichService:
    async def check_asset_exists(self, content_hash: str) -> Optional[str]
    async def upload_asset(self, image_data: bytes, filename: str) -> str
    async def get_asset_metadata(self, asset_id: str) -> dict   # EXIF, faces, objects, OCR, geo
    async def get_thumbnail_url(self, asset_id: str) -> str
    async def list_new_assets(self, since: datetime) -> list[dict]
    async def search(self, query: str, since=None, until=None) -> list[dict]  # retrieval tool
```
Config (new env): `IMMICH_URL=http://immich-server:2283`, `IMMICH_API_KEY=...`. Add to `.env.template` and wizard.

### 4c. Serving proxy
`GET /api/images/{event_id}/blob` → if `storage=cas`, stream from `BlobStore`; if `storage=immich`, proxy/redirect to Immich original/thumbnail. `Cache-Control: immutable` (content-addressed).

---

## 5. Ingestion & enrichment pipelines

### 5a. Screenshot path (wearable → CAS → segments → events)
1. **`POST /api/images/frames/batch`** — wearable uploads `events.jsonl` + referenced JPEGs (multipart or tar). For each `displays[]` entry: `changed:true` → `BlobStore.put_bytes`, create `CaptureFrameDocument` (status from OCR presence); `changed:false` → frame doc pointing at the prior blob (CAS dedups), inherit later. Idempotent on `(user_id, ts, display_index, content_hash)`.
   - Wearable uploader is **net-new** (current `backend_sender.py` is audio-only). Walk per-day folders + `events.jsonl`, reuse its JWT auth.
2. **Segmentation job** — group a user's contiguous `capture_frames` by `(app, time-gap > N s, max-len)` → one `ImageEvent(kind="screen_segment")`. Pick a representative frame (most OCR / last changed). Aggregate OCR + app/title/url. Set `segment_id` on member frames.
3. **Caption** the representative frame via **Gemma4 vision** (HTTP to the existing vision service — heavy deps stay out of the backend image).

### 5b. Photo path (Immich is the backend)
- **Upload** `POST /api/images/upload`: hash → `check_asset_exists` → push to Immich if absent → create `ImageEvent(kind="photo", storage="immich")` → pull metadata.
- **Sync** `POST /api/images/sync_from_immich`: `list_new_assets(since=last_sync)` → create `ImageEvent`s for new assets → enqueue enrichment. (Trigger: manual now; cron later.)
- **Enrichment = `ImmichProvider`**: faces → `detected_people`, plus objects/OCR/EXIF/geo. Optional `VisionLLMProvider` (Gemma4/Claude) for richer captions — pluggable `ImageAnalysisProvider` ABC (`services/image_analysis/`).

### 5c. Job conventions (`workers/image_jobs.py`, new)
`@async_job(redis=True, beanie=True)`, async, imports inside fn, **return dict / never raise**. Reuse the `default` or `memory` queue — the 6 `rq-worker-*` already serve it (no worker-registration change). Enqueue helpers modeled on `enqueue_memory_processing` (`workers/memory_jobs.py:521`).

---

## 6. Memory plane: promotion + vault embedding (the bridge)

After a screen-segment is captioned, or a photo is enriched, feed it to memory **through the vault agent**:

```python
memory_service = get_memory_service()
await memory_service.add_memory(
    visual_text,                       # caption + aggregated OCR + app/url (or photo desc + geo)
    client_id, event_id,               # event_id is the source_id
    user_id, user_email,
    source_type="image",               # NEW param -> tells the agent it's visual
    extra_metadata={                   # NEW param
        "image_refs": ["cas:<hash>"] or ["immich:<asset_id>"],
        "captured_at": ...,
        "people": detected_people,     # Immich faces
        "location": location,
    },
    allow_update=True,
)
```

- **`add_memory` gets `source_type` + `extra_metadata`** params (`services/memory/base.py:95`; provider `services/memory/providers/chronicle.py`). This was in the old plan — still correct.
- **The agent is the promotion gate.** Its system prompt for `source_type="image"` instructs: judge memory-worthiness; if worth it, write/update the relevant note and **embed the image**; if Immich supplied `people`, link those `[[Person]]` notes (Kepano person graph). If not memory-worthy → do nothing (stays context-only). Set `ImageEvent.promoted`, `vault_note_path`, `vault_media_path` on success.

### Vault embedding mechanism (the one real surgical change to vault tooling)
- **`_media/` folder** in the per-user vault, content-addressed (`_media/<sha256>.jpg`) → dedup in the vault. Because the Syncthing boundary is the whole vault dir with only `.obsidian` ignored (`vault_sync_routes.py:40`), `_media/` **syncs to every Obsidian device** — correct for a curated subset, and exactly why the firehose must stay in CAS *outside* `conversation_docs`.
- **New `VaultTool`: `attach_image(image_ref) -> "_media/<hash>.<ext>"`** (`services/memory/agent/vault_tools.py`): copies from CAS or downloads from Immich into `_media/`, returns the embed path; the agent then writes `![[_media/<hash>.jpg]]` via `write_note`/`edit_note`. Add its OpenAI schema to `VAULT_TOOL_SCHEMAS` (vault_tools.py:419-598) and mention it in the agent prompt/templates.
- **Relax `_safe_relpath` (`vault_tools.py:54`)** — today it force-appends `.md` and rejects paths >1 deep, so binary `_media/` writes are impossible. Special-case `_media/<file>.{jpg,png,webp}`. Keep `.md` enforcement for notes.
- Inbound Syncthing audit listener is `.md`-only (`syncthing_audit.py:75`) — image edits aren't audited; fine.

---

## 7. Timeline & retrieval

- **`GET /api/images/timeline`** (or a unified `/api/timeline`) merges `Conversation` + `ImageEvent` by time into one sorted feed — the first real merged event timeline (there is no existing timeline feed; the Knowledge Graph entity/timeline service was removed). Merge `image_events` only (NOT raw frames → stays lean).
- **Photos-near-conversation**: `ImageEvent.find(user_id, captured_at ∈ [conv.start±window])` → filmstrip on the conversation detail page (`webui/src/pages/Conversations.tsx`, card map ~line 1150; audio fetch pattern at line 269).
- **Immich as a retrieval tool**: give the vault-aware search agent an `immich_search` tool ("photos of Alice near 3pm Tuesday") → `ImmichService.search`.

---

## 8. Wiring map (exact integration points)

All paths under `backend/src/backend/`.

| What | Location | Action |
|---|---|---|
| Register Beanie models | `app_factory.py:146-156` (`init_beanie document_models=[...]`) | Add `CaptureFrameDocument`, `ImageEvent`. |
| Worker Beanie init | `models/job.py` (`@async_job(beanie=True)` → `_ensure_beanie_initialized`, ~:32/279) | **Verify** whether it uses the app_factory list or its own; if its own, add both models or `image_jobs` can't query them. |
| New router | `routers/modules/image_routes.py` (new) | `router = APIRouter(prefix="/images", tags=["images"])` → served at `/api/images`. |
| Export + mount router | `routers/modules/__init__.py:24,45` + `routers/api_router.py:13-31,41-43` | `from .image_routes import router as image_router`; `router.include_router(image_router)`. |
| CAS data dir | `app_config.py:45-46` + `:102-104` | `self.cas_blob_dir = Path(os.getenv("CAS_BLOB_DIR","./data/cas")); mkdir(...)` + `get_cas_blob_dir()`. |
| Volume mount | `docker-compose.yml` (backend + `worker_orchestrator` at `:79`) | Mount CAS dir (slow HDD) into both; container path must match `CAS_BLOB_DIR`. NOT under `data/conversation_docs`. |
| Immich config | `.env.template`, wizard (`backend/init.py`) | `IMMICH_URL`, `IMMICH_API_KEY`. |
| Auth deps | `auth.py:166-168` | `current_active_user` / `current_superuser`. |
| RQ queues | `controllers/queue_controller.py:78-101` | Reuse `default`/`memory`; new queue only if isolation wanted. |
| Job pattern | `workers/memory_jobs.py:123-129`, `models/job.py:227` | `@async_job(redis=True, beanie=True)`, return dict, never raise. |
| Memory `add_memory` | `services/memory/base.py:95` + `providers/chronicle.py` | Add `source_type` + `extra_metadata` params; thread into MemoryEntry/agent context. |
| Vault agent prompt | `services/memory/agent/memory_agent.py:74-161` | Add image-source branch: judge promotion, embed via `attach_image`, link `[[People]]` from Immich faces. |
| Vault tools | `services/memory/agent/vault_tools.py` (`VaultTools` :105, `_safe_relpath` :54, schemas :419-598) | Add `attach_image` tool; relax `_safe_relpath` for `_media/`. |
| Vault templates | `services/memory/agent/vault_templates.py` | Optionally add an `## Images` / embed convention. |
| Style refs | `models/audio_chunk.py` (model), `routers/modules/audio_routes.py` (range serving) | Mirror. |

---

## 9. Implementation phases

**Phase 1 — CAS + frame firehose (context plane, screenshots):**
- `services/cas/blob_store.py`; `app_config` CAS dir; docker-compose volume.
- `models/capture_frame.py` + Beanie register; `POST /api/images/frames/batch`; `GET /api/images/{id}/blob` (range); wearable uploader in `extras/local-wearable-client`.
- Smoke: upload `events.jsonl`+JPEGs, fetch back, confirm dedup.

**Phase 2 — segments + timeline:**
- `models/image_event.py` + register; segmentation job (frames → `screen_segment` events); Gemma4 caption.
- `GET /api/images/timeline` (merge conversations + image_events); photos-near-conversation query.

**Phase 3 — Immich (photos):**
- `services/immich/client.py`; `services/image_analysis/` (ABC + `ImmichProvider` + `VisionLLMProvider`); `POST /api/images/upload` (→Immich), `POST /api/images/sync_from_immich`.

**Phase 4 — memory plane bridge (promotion + embedding):**
- `add_memory` `source_type`/`extra_metadata`; agent image-branch + `attach_image` tool + `_safe_relpath` relax; Immich faces → `[[Person]]`.
- Feed screen-segments + photos to `add_memory`. Verify worker Beanie init.

**Phase 5 — frontend + lifecycle:**
- Timeline view merging conversations + images; filmstrip; rename Upload→Ingress; Immich search tool for retrieval agent.
- Blob GC job; frame retention pruning.

---

## 10. Open decisions (deferred, non-blocking)
- **Immich auth**: single server API key (simplest, self-hosted) vs per-user keys.
- **Vision enrichment**: Immich ML only, or also Gemma4/Claude vision for richer captions (configurable like `LLM_PROVIDER`).
- **Sync frequency**: manual → cron → Immich webhooks.
- **Frame retention**: how aggressively to prune `capture_frames` after segmentation.
- **v1 search**: text-only (caption+OCR → vault notes) vs CLIP image embeddings later.

## 11. Testing
- `tests/TESTING_GUIDELINES.md` + approved tags only (`infra`; do not invent). Gate Gemma4/Immich paths like `requires-api-keys`.
- Unit-test `BlobStore` (dedup, atomic write, sharded path) without Mongo.
- Endpoint tests (upload/serve/timeline) under `make test-no-api`.
