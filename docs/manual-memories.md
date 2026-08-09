# Manual memories

A manual memory is something a person deliberately saves to Chronicle. The memory is
the user action; its attachments are media that Chronicle may enrich afterward.

## Creation contract

The mobile share sheet posts images to `POST /api/manual-memories`. The multipart
request contains repeated `attachments`, a client-generated `request_id`, and an
optional `note`. The backend validates the real image bytes, writes each original to
the user's content-addressed `_media/` directory, creates one searchable
`Manual Memories/<memory-id>.md` note, and inserts the memory before returning `202`.

Queue or model availability cannot change a successful share into a failure. The
original and the user's note are available immediately.

`request_id` makes retries idempotent. Sharing identical bytes in a new request creates
a new memory and reuses the stored content. This preserves distinct notes and intent.

## Data model

`ManualMemory` owns the note, provenance, `shared_at`, optional `memory_at`, vault path,
and embedded attachment records. Each attachment has its own ID, content hash, storage
path, and separate states for description, extracted text, and visual indexing.

The first slice supports JPEG, PNG, and WebP images up to 10 MiB each. The API and model
accept multiple attachments; the current phone confirmation screen sends one image.

## Enrichment and recovery

`manual_memory_image_enrichment` describes images and extracts text asynchronously.
`manual_memory_visual_index` sends images to ColPali when it is available. Both are cron
backstops for missed or interrupted on-arrival work; failures never hide the memory.

The user's note is written first in the vault note and remains the primary Timeline and
search description. Generated interpretation and extracted text are secondary.

## Browsing, Timeline, and chat

`GET /api/manual-memories` returns the authenticated user's collection ordered by
`shared_at`, independent of semantic Timeline analysis or the selected day. Timeline's
Manual memories panel uses this endpoint.

Manual memories shared during a day are also deliberate `user_action` evidence for
Timeline analysis. Chat citations to `Manual Memories/<memory-id>.md` resolve through
the authenticated memory and attachment endpoints, never through a content hash alone.

## Active-development reset

There is no compatibility reader for the former screenshot API or `kind="screenshot"`
device-input documents. Before deploying this model, an operator should export anything
worth keeping, then remove those old documents, their `Media/<digest>.md` notes, and old
ColPali entries. Do not perform this reset without explicit authorization.
