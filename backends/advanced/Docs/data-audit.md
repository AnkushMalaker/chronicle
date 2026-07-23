# Data Audit

Admin tooling for inspecting and curating recorded audio: VAD-based speech
metrics, speech-aware preview, conversation split/merge, and audio archival.
WebUI page: `/data-audit` (admin only). Backend prefix: `/api/data-audit`.

## VAD analysis

Speech detection runs through a swappable provider layer
(`services/vad/` — `VADProvider` ABC + registry). Default provider is
**TEN VAD** (`ten_vad`, pinned git dep; numpy-only, CPU, 16 kHz, one score per
256-sample / 16 ms frame). Select via `data_audit.vad_provider` in config.
Adding a provider = one module + registry entry; torch-based providers belong
in `extras/` services, not the backend image.

`utils/vad_analysis.py::analyze_conversation_audio()` decodes the stored opus
chunks in 30-chunk batches and writes results at two levels:

- **Per chunk** (`AudioChunkDocument.vad`, a self-contained `VADResult`):
  `provider`, `frame_hop_ms`, `scores` (per-frame probabilities), `max_score`,
  `threshold`, `has_speech`. Self-contained because chunks (and their scores)
  survive split/merge while the conversation-level summary does not.
- **Per conversation** (`Conversation.vad_analysis`, `VadAnalysis`):
  a 20-bin probability histogram (derive speech-% at any threshold without
  touching chunks), plus `speech_regions` — merged `[start, end]` speech
  intervals (padded ±0.3 s, gaps < 3 s merged, blips < 0.4 s dropped,
  capped at 500 by doubling the merge gap).

Analysis is enqueued via `POST /api/data-audit/analyze`
(`analyze_audio_batch_job`; skips analyzed conversations unless `force`) and
is idempotent/resumable — running it with no ids backfills the whole corpus.
The `auto_clean` cron archives speech-free conversations using
`data_audit.auto_clean` thresholds (`speech_prob_threshold`,
`max_speech_fraction`, `min_duration`, `max_archive_per_run`).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /analyze` | Enqueue batch VAD analysis (poll `/api/queue/jobs/{id}/status`) |
| `GET /conversations` | Filtered listing: `speech_threshold`, `min_speech_fraction`/`max_speech_fraction`, `min_duration`/`max_duration`, `min_silence_gap`, `created_after`/`created_before`, speaker include/exclude, `archived_only`. Each row carries `max_silence_gap_seconds` (longest interior gap from cached `speech_regions`) |
| `GET /speakers` | Distinct speaker labels |
| `POST /archive` | Hard-delete audio bytes, keep metadata stub |
| `GET /conversations/{id}/silence-gaps` | Long speech-free gaps (split candidates) from the merged VAD speech regions (same definition as the listing filter). At the default threshold it serves cached `speech_regions`; at a custom threshold it re-derives from chunk frame scores. Flags cache drift (see below) and refuses to offer a split over an inconsistent chunk set |
| `GET /conversations/{id}/speech-regions` | Merged speech intervals for speech-skip playback; derives from chunk scores and caches back if missing (no audio decode). Optional `speakers` (comma-separated labels): intersects the raw frame intervals with those speakers' transcript segments *before* merging, returning only time where the VAD heard voice while one of them was tagged (never cached) |
| `POST /conversations/{id}/split` | Split at time points (snapped to 10 s chunk boundaries) |
| `POST /merge` | Merge adjacent conversations into a new one |
| `GET /sensitivity-policy` | Configured default shareability policy (prefill for the screen editor) |
| `POST /export/screen` | Enqueue a privacy screen over `conversation_ids` against a `policy` (see below) |
| `POST /export` | Enqueue an annotation-dataset export (see below) |
| `GET /exports` · `GET /exports/{id}/download` · `DELETE /exports/{id}` | List / download (`?token=` supported) / delete exports |

## Split / merge semantics

Both are inline Mongo operations — audio is never decoded or re-encoded;
chunk documents are reassigned with pipeline `update_many` (re-id, re-index,
shift times). Transcripts are sliced/concatenated from the active version by
time range (`utils/transcript_slicing.py`), so segments keep their speaker
labels; memory extraction + title/summary re-run via
`start_post_conversation_jobs(..., skip_speaker_recognition=True)`.

- **Split**: children are created first (crash-safe without transactions),
  each with `derived_from` lineage; the parent is soft-deleted
  (`deletion_reason="split"`, `derived_into=[child ids]`) and its memories
  best-effort deleted (`MemoryServiceBase.delete_memories_by_source`).
  Gap audio stays with the preceding child.
- **Merge**: requires same user + client and adjacency (no other live
  conversation in between — server-checked). Creates a **new** conversation
  (`created_at` = earliest source); wall-clock gaps between recordings are
  elided and recorded as a NOTE seam segment. Sources are soft-deleted with
  `deletion_reason="merged"`.
- Re-splitting/merging an already-derived source is rejected via
  `derived_into` (409).

## Filter system (WebUI)

The page's filters are driven by a declarative registry,
`webui/src/components/dataAudit/filters.tsx` (`AUDIT_FILTERS`): each filter
defines its default value, active-test, chip label, query params, and popover
editor. `AuditFilterBar` renders a "＋ Filter" menu plus one chip per active
filter; edits auto-apply when a popover closes. Current filters: Speech %
(min/max + VAD threshold), Duration (min/max), Speakers (tri-state
include/exclude), Date range. **Adding a filter** = one registry entry +
(if server-side) one query param in `GET /conversations` and one predicate
line in `data_audit_controller.list_for_audit`. Speech bounds hide
unanalyzed conversations; the date range narrows the Mongo scan itself
(helps with the `MAX_SCAN` cap on large corpora).

The **Silence gaps** filter (`min_silence_gap`, seconds) keeps only
conversations with a long interior silence gap — split candidates. It's derived
from each conversation's cached `vad_analysis.speech_regions`
(`silence_gaps_from_regions`, no chunk reads) and also hides unanalyzed
conversations. When this filter is active and rows are selected, the toolbar's
**Split at gaps** button (`BulkSplitModal`) previews the gaps per conversation
(via `silence-gaps`) and then splits each at all its gaps, looping the
per-conversation `split` endpoint.

**One silence definition everywhere.** The filter, the split preview and the
split all use the *same* merged-speech-region detector
(`detect_silence_gaps(speech_regions, …)` over TEN-VAD frame-derived regions),
so a row that's flagged as having a gap is actually splittable at that gap. This
replaced an earlier per-chunk `max_score` test in the split path, which a single
loud frame per 10 s chunk defeated on ambient audio — making the filter and the
splitter disagree. (The frame scores are TEN VAD neural speech probabilities,
not RMS energy.)

**Cache-drift guard.** A conversation with cached `vad_analysis` but chunks that
don't back it (unscored chunks, or a cache duration that no longer matches the
chunk set — e.g. left by the reconnect-duplicate dedup) is *drift*, not a normal
"not analyzed yet". `silence-gaps` detects it via a cheap unscored-chunk probe,
records a `data_integrity` **system event** (`_report_vad_drift` → System Errors
page), and refuses to offer a split over the inconsistent chunk set
(`needs_analysis`), rather than producing garbage children.

## Annotation export

`POST /export` (body: `conversation_ids`, `mode` `clips`|`full`,
`pad_seconds` default 1.0, `speech_threshold` 0.5, `merge_gap_seconds` 3.0)
enqueues `export_annotation_dataset_job`. Mode `clips` (default): per
conversation it derives speech regions from the chunk frame scores with the
requested padding (running VAD inline for unanalyzed audio) and cuts one
sample-accurate WAV clip per region (`reconstruct_audio_segment`). Mode
`full`: one untouched WAV per conversation, no VAD involved. Either way each
clip is paired with the sliced active transcript.
Output lands in `data/exports/{export_id}/` as `dataset.zip`
(`audio/{conversation_id}_{idx:03d}.wav` + `manifest.jsonl` + `export.json`)
plus an `export.json` copy outside the zip for cheap listing.

Each `manifest.jsonl` record carries `clip_id`, clip-relative `segments`/
`text`, `source_start_seconds`/`source_end_seconds` (absolute position in the
source conversation), and an empty `annotation` block —
**the round-trip contract**: the future import endpoint matches annotated
records by `clip_id` + `conversation_id` + source times. Helpers live in
`utils/annotation_export.py`; the UI entry point is the **Export…** button on
the Data Audit toolbar (exports selected rows; modal also lists/downloads/
deletes server-side exports).

## Privacy screen (shareability gate)

Before sharing audio + transcripts with an outside annotator, the export can
run a configurable **shareability screen** — flagging segments too *personally
sensitive* to send. This is deliberately **not** a PII redactor: names and
identifiers are kept (annotators need them for speaker labels). The bar is the
user's own comfort, driven by an editable policy prompt — and it covers more
than PII (family/health/finances, confidential deals, etc.).

Two-phase, review-before-finalize:

1. **Screen** — `POST /export/screen` (`conversation_ids`, optional `policy`)
   enqueues `screen_conversations_job`. For each conversation an LLM
   (`sensitivity_screening` operation, JSON output) applies the policy to the
   active transcript's speech segments and returns the flagged ones with their
   **absolute time range**, quote, category, and reason. The job only screens —
   nothing is exported or mutated. Default policy: `data_audit.export.sensitivity.policy`
   (config), exposed via `GET /sensitivity-policy`.
2. **Export with exclusions** — `POST /export` accepts
   `excluded_ranges` (`{conversation_id: [[start, end], …]}`, the ranges the
   user confirmed) and `sensitivity_policy` (recorded in metadata). The export
   job carves the excluded ranges out of each conversation's regions
   (`vad_analysis.subtract_intervals`, applied *after* padding so padding can't
   re-expose a cut), so the withheld **audio and transcript** never enter a
   clip. `export.json` records `params.screened`, `params.sensitivity_policy`,
   per-conversation `excluded_seconds`, and `totals.excluded_seconds`.

Segment-level granularity: a single conversation's non-sensitive parts still
export normally; only the flagged ranges drop out (splitting a clip in two if a
cut lands mid-region). Logic lives in `utils/sensitivity_screening.py` (pure
prompt/parse) + `workers/data_audit_jobs.py` (`screen_conversations_job`,
`_export_conversation_clips`). The UI flow is in `ExportModal.tsx`: enable the
screen, edit the policy, **Run privacy screen**, review the flagged segments
(each a withhold checkbox, default on), then **Export**.

## Audio preview (WebUI)

`components/dataAudit/PreviewStrip.tsx` renders a speech timeline (blue =
speech regions; in the split modal amber = silence gaps, red = chosen split
points) with a dual-mode player: **Speech only** plays just the regions,
skipping silence; **Full audio** plays contiguous 60 s windows. Both modes
fetch **exact time-clipped WAV** from
`GET /api/audio/chunks/{id}?start_time&end_time&format=wav`.

A **speaker filter** dropdown (shown when the row has speaker labels) limits
playback to one speaker: regions become the VAD ∩ speaker-segment overlap
(violet on the timeline), so listening skips both silence and everyone else.
Picking a speaker switches to speech-only mode and resumes from the current
position, snapping forward to that speaker's next region.

**Important**: never drive a browser `<audio>` element from
`GET /api/audio/get_audio/{id}?format=opus` if you need seeking or a correct
duration — the stored opus is one independent ogg stream per 10 s chunk, and
the concatenation is a "chained ogg" that browsers (and ffprobe) parse as
only the first stream. Use the chunks endpoint with `format=wav` (sample-
accurate server-side clipping) or windowed playback instead.
