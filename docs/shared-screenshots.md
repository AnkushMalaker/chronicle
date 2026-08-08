# Shared screenshots

Seeing something worth remembering and screenshotting it used to be a dead end: the
image sat in the camera roll and Chronicle never learned about it. Sharing a
screenshot to the Chronicle app now makes it a first-class, queryable memory.

The act of sharing is the point. Chronicle does not ingest a camera roll — each image
is picked deliberately, and that choice is also the privacy gate: everything shared is
described and fully text-searchable.

## The path

```
phone share sheet
   → share extension hands the image to the app
   → confirm screen (optional note)
   → POST /api/device-input/screenshots      ← durable here, in under a second
        ├─ media_data inline in Mongo        (serving)
        └─ _media/<sha256>.png in the vault  (durable original)
   → describe pass (Codex vision, on arrival)
        ├─ item metadata: description, ocr_text, app_or_site, entities, tags
        └─ vault note Media/<sha256>.md      ← what makes it findable
   → embed pass (ColPali on a GPU node, when it is awake)
```

Two properties are load-bearing:

**The upload returns before anything understands the image.** Describing is a Codex
round trip of tens of seconds. The endpoint's contract is that the screenshot is
durable when it returns, not that it has been read. Both copies are written
synchronously, so a screenshot survives even if every downstream stage fails forever.

**The vault note is what makes retrieval work.** Vault search is ripgrep over
Markdown, so an image is findable only if something writes words about it. With the
description and verbatim OCR in `Media/<digest>.md`, the *existing* memory search
answers "find the screenshot of the concert ticket" with no new machinery — no GPU, no
visual index.

## Ingest

`POST /api/device-input/screenshots` — JWT-authenticated, multipart.

| Field | Notes |
|---|---|
| `file` | JPEG, PNG or WebP. HEIC is rejected: the backend has no image library, so conversion belongs on the phone, which already has one. |
| `captured_at` | The photo's timestamp; defaults to now. |
| `caption` | Optional, ≤2000 chars. The highest-value retrieval signal, and the one thing no vision model can reconstruct. |
| `origin_app` | Optional, best-effort. |

Returns `202 {"status": "accepted"|"duplicate", "item_id", "content_hash"}`.

The phone does **not** pair. It already holds a JWT; pairing would mint a second
long-lived credential on the same device for nothing. A synthetic `CaptureSource`
(`mobile-<user_id>`) is created on first share, with a deliberately unmatchable
`token_hash` so the device-token path can never authenticate as it.

Dedupe is free: the sha256 digest becomes `source_item_id`, and the existing unique
`(user_id, source_id, kind, source_item_id)` index makes a re-share or a client retry
idempotent without any client-supplied token.

The 10 MiB cap is deliberately distinct from `_MAX_IMAGE_BYTES` (25 MiB), which
already exceeds MongoDB's 16 MiB document limit and must not be reused for a field
stored inline.

## Describing

Configured under `screenshots` in `config/defaults.yml`, shaped like `memory.agents`:

```yaml
screenshots:
  agents:
    describe:
      backend: codex        # codex | pi (pi is a declared slot, not yet implemented)
  backends:
    codex:
      model: "gpt-5.6-luna"
      reasoning_effort: low
      timeout_seconds: 300
```

Selecting `pi` fails loudly rather than falling back — a silent fallback would change
which model reads your images with nothing saying so.

Runs on arrival as an RQ job, with the `screenshot_descriptions` cron (every 5 min) as
a backstop. Both can reach the same item, so the claim is a conditional update:
whichever gets there first moves `description_state` to `describing` and the other
sees zero modified documents and skips. Exactly one vision run per screenshot.

**Codex being unavailable is a service fault, not an item fault.** It says nothing
about the image, so the claim is released and no attempt is consumed. Only genuine
per-image failures count toward the 3-attempt limit.

The vision call itself is `services/vision/codex_vision.py`, shared with the timeline's
episode-thumbnail picker — the two are the only vision paths in Chronicle and they use
one seam rather than two copies of the subprocess handling.

## Retrieval

**Timeline.** A screenshot becomes evidence with `kind: "frame"` and
`role: "user_action"` — a deliberate act, not whatever happened to be on screen. Its
caption and description carry into the evidence excerpt; `ocr_text` is deliberately
excluded from the metadata payload because it can run to thousands of characters and is
already in the excerpt.

**Chat.** Cited `Media/<digest>.md` notes render as thumbnails under the assistant's
message. The digest in the note filename *is* the image's content hash, so a citation
is enough to fetch the picture — nothing extra is threaded through the search path, and
it still works after a reload. Images are fetched as authenticated blobs
(`GET /api/device-input/media/{digest}/thumbnail`); a bare `<img src>` sends no
Authorization header and would 401.

**Visual search** (`search_images`) is an additive tool on the memory agent, backed by
the ColPali service. It returns `Media/` note paths, so the agent's natural next move
is `read_note` — which the search loop already records as evidence and surfaces as a
citation. Nothing else had to learn about images.

When the visual service is unreachable the tool returns a plain sentence saying so and
pointing at grep; it never raises, so the agent recovers within the same round and the
answer is still correct from the descriptions.

## The visual index

See [`extras/colpali-service/README.md`](../extras/colpali-service/README.md).

It is deliberately built second and is never required. Late-interaction retrieval earns
its place only on the visual tail — "the one with the blue chart" — that defeats both
a written description and OCR.

**The backend pushes; the service does not pull.** `DeviceInputJob` is keyed by
`source_id` and its puller authenticates a capture-device token, but a GPU node
produces no device input. Pull would mean inventing a compute-node provider, a pairing
flow, a long-lived token on a GPU box, *and* a new device-token endpoint serving image
bytes — real new attack surface for nothing. Push is what ASR, TTS and speaker
recognition already do.

The node is a desktop that sleeps. So `screenshot_embeddings` (every 10 min) probes
health first, and an unreachable service ends the tick **without touching any item's
state** — a sleeping GPU must never consume a screenshot's retry budget. Each tick also
reconciles against `GET /documents`, so a wiped index volume or a `COLPALI_MODEL`
change re-queues the affected screenshots instead of leaving a silent permanent gap.

## Mobile

The share extension does **not** upload. It hands the image to the app, which uploads
with the JWT already in its secure storage — which is why no shared keychain access
group is needed, and why the confirm screen exists to catch a caption.

`expo-share-intent` creates the iOS extension target (there is no
`withXcodeProject` precedent in this repo to hand-roll one) and the Android
`ACTION_SEND` filters. It derives the app group `group.com.cupbearer5517.chronicle`
and the extension bundle id `com.cupbearer5517.chronicle.share-extension`.

The app normalises to JPEG at 2048px before upload, which handles HEIC and keeps every
share far under the size cap.

**There is no offline queue.** A failed upload keeps the confirm screen open with an
inline Retry; a share attempted with no connectivity is lost. An explicit v1 decision,
not an oversight.
