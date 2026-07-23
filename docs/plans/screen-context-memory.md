# Event-Driven Screen and Audio Memory Curation

## Summary

Chronicle should maintain two representations of device context:

1. A compact system observation layer containing event-driven screen metadata,
   incremental context samples, sparse previews, audio links, Immich candidates, and
   curation outcomes.
2. A curated vault layer containing only durable knowledge and selected images that the
   Codex memory agent judges useful.

A long uninterrupted activity is one observation, but not one payload. For example, six
hours in the same editor remains one activity span while materially different code,
documentation, terminal, or browser states are appended as context samples. The backend
can process those samples incrementally and decide which details deserve memory.

ScreenPipe and Immich remain the high-resolution sources of truth. Chronicle does not
mirror complete frame streams or photo libraries.

## Observation lifecycle and send timing

### Meaningful context boundaries

The local collector identifies an activity by `(app, window title, browser URL)`. Every
switch opens a local candidate immediately; the collector does not throw away the first
10 seconds. A 10-second stability window delays the remote `open` send so passive
launchers, notifications, task switchers, and other unmodified transient windows can be
folded into the surrounding activity.

If a short-lived candidate receives keyboard/mouse interaction, changes its visible
content or title, starts/stops media, or otherwise produces a meaningful ScreenPipe
capture trigger, it is finalized and sent as its own short observation when the user
switches away—even when it lasted less than 10 seconds. That meaningful excursion closes
the prior uninterrupted observation, and returning opens a new observation for the prior
app. The backend may relate or deduplicate those adjacent observations, but the collector
never discards the intentional short interaction.

The collector sends:

- An `open` upsert after a new context remains stable for 10 seconds. This includes the
  activity identity, start time, an initial bounded text sample, and ranked local frame
  pointers.
- A `sample` upsert while the same activity remains open when its accessibility/OCR text
  changes materially. Text is normalized and fingerprinted locally; identical or
  near-identical content is not resent. Samples have a two-minute minimum cooldown only
  within that unchanged observation. A switch to another app/window is a new candidate
  and is never blocked by this cooldown.
- A liveness sample after 15 minutes without a prior sample when the device is active.
  This is a safety boundary for long editing, reading, video, or terminal sessions; it
  appends to the same observation and does not create a new observation or memory.
- A `close` upsert on a meaningful switch, lock, capture pause, or graceful service
  shutdown. It includes the final duration, final useful text state, and the best frame
  candidates seen across the activity.

An all-day unchanged window therefore produces one observation with incremental samples,
not a series of five-minute or 30-minute observations. A truly static or idle screen
produces only lightweight liveness updates, which the backend can mark as duplicates.

The open observation and unsent samples are persisted in the collector state directory.
After a crash or restart, the collector resumes or finalizes that observation using
ScreenPipe's local frame history and its saved cursor.

### Context sample contents

Each sample contains only compact context:

- capture timestamp and elapsed activity duration
- app, window title, browser URL, and capture trigger
- bounded accessibility/OCR excerpt, normalized before fingerprinting
- content fingerprint and previous-sample relationship
- first, last, and representative ScreenPipe frame IDs
- idle, locked, blank, or DRM-paused markers when available

The collector retains up to three ranked frame candidates per observation. Ranking uses
dwell time, useful text, nonblank metadata, distance from a switch boundary, and visual or
textual novelty. Frame IDs are source pointers; pixels are not uploaded with every sample.

## Backend observation model

Add `observation` to `DeviceInputItem.kind` and represent lifecycle separately from
curation:

- lifecycle: `open` or `closed`
- curation: `pending`, `curating`, `discarded`, `duplicate`, `linked`, `promoted`, or
  `failed`

An observation stores its ordered context samples, ranked frame candidates, related audio
conversation IDs, duplicate target, curation revision, agent reason, vault paths, and
media provenance. Use the ScreenPipe source ID plus the observation's first frame ID as
the idempotency key.

Expose authenticated device endpoints for opening/upserting and closing observations.
Retries must update the existing observation and deduplicate samples by fingerprint and
timestamp rather than creating additional records. The existing source heartbeat handles
service liveness; heartbeats never create observations.

The timeline response exposes the observation span, current/open state, samples, preview,
related conversations, duplicate target, and vault outcome.

## Sparse screenshot selection

The backend automatically requests at most one 640px preview from the observation's best
frame candidate. It does not request a preview for idle, locked, DRM-paused, blank, or
metadata-poor observations.

The curation agent may request one alternative preview if the first is unusable and a
different ranked candidate exists. Thus an observation normally transfers zero or one
image and has a hard automatic limit of two previews, even when it lasts all day.

If the agent elects to retain a ScreenPipe image, the backend requests a bounded 1280px
still from the local source. If the source is offline or the original has expired, it may
promote the existing preview. No full frame sequence is transferred.

For Immich, Chronicle inspects metadata and low-resolution thumbnails first. It fetches an
original asset only after the agent explicitly chooses to promote that image.

## Audio correlation and retention decisions

ScreenPipe audio capture and forwarding remain independent of screen observations.
Chronicle continues to run VAD and transcription on received audio, then correlates each
completed audio session with observations from the same source and overlapping timestamps.
Microphone and system-output streams remain separate.

The curation agent receives:

- new observation samples since its previous revision
- app/window/URL context and elapsed duration
- available preview images
- related audio direction, transcript segments, and conversation summaries
- nearby Immich asset metadata and thumbnails
- relevant existing vault-note summaries

System-output dialogue must be interpreted as media content rather than personal speech.
For a show, film, game, or song, the agent may retain the title, episode, progress, and
explicit user reactions, but must never turn character dialogue into facts about the user.
Microphone speech is considered separately and facts are attributed only when speaker
evidence supports that attribution.

When an open observation accumulates novel samples, the backend coalesces them into a
curation revision. It invokes the agent at most once per 15 minutes while the observation
is open and once more when it closes. Exact duplicate samples are eliminated before this
stage. The agent may write nothing, update an existing note, or record only a subset of
the supplied context. Deterministic revision IDs prevent repeated jobs from repeating
vault edits.

The agent classifies each revision as:

- novel and worth recording
- related to an existing observation, conversation, or vault note
- duplicate of a canonical observation
- routine or low-value and discarded

Duplicate observations remain as compact audit markers linked to the canonical item.
Their redundant preview bytes are deleted and they do not trigger another vault write.

## Vault representation and media promotion

Raw observations remain in Chronicle's database and are never represented as fake audio
conversations.

The agent writes useful routine context into `Daily/YYYY-MM-DD.md`. Durable experiences
such as parties, trips, meetings, projects, games, shows, or recurring research receive
appropriate event, project, topic, place, or media notes. Observations link to overlapping
real conversations instead of duplicating transcript content.

Promoted images are content-addressed at `_media/<sha256>.<ext>` and embedded with Obsidian
links. Note frontmatter records:

- source provider (`screenpipe` or `immich`)
- source ID and frame or asset ID
- capture time
- related observation and conversation IDs
- content hash

Media is deduplicated by content hash across ScreenPipe and Immich. Promoted vault media
is retained independently, while the local services remain the source for higher-resolution
or additional context. Failed source retrieval remains retryable and must not leave partial
notes or media files.

## Privacy and retention

- Preserve ScreenPipe PII removal and bounded text excerpts.
- Never send ScreenPipe's SQLite database, complete OCR history, or complete frame stream.
- Keep compact observation metadata and duplicate decisions as the durable audit record.
- Delete backend previews after discard or duplicate classification unless they are linked
  to another retained observation.
- Do not delete ScreenPipe or Immich originals during backend cleanup.
- If the visual Codex executor is unavailable, leave visual curation pending rather than
  falling back to a lower-confidence text-only promotion decision.

## Implementation areas

### Collector

- Replace remote per-transition activity uploads with observation lifecycle upserts.
- Add local text fingerprinting, novelty comparison, cooldown handling, sample buffering,
  candidate ranking, and crash recovery.
- Continue querying ScreenPipe locally on the existing short polling loop; polling does not
  imply remote transmission.

### Backend

- Extend the device-input model and API with observations, samples, lifecycle, curation,
  duplicate links, and vault outcomes.
- Change automatic thumbnail creation from every salient activity to sparse observation
  previews.
- Correlate processed ScreenPipe audio and discovered Immich assets by timestamps.
- Add idempotent, revision-based observation curation jobs and source-agnostic media
  promotion.

### Agent

- Add an observation-curation prompt and executor contract separate from the conversation
  memory contract.
- Allow the Codex executor to inspect supplied previews, existing vault structure, related
  conversations, and Immich candidates.
- Require explicit structured decisions before vault or media mutation.

### UI and operations

- Display observations as evolving spans with their sample count, duration, preview,
  related audio, duplicate target, and vault outcome.
- Add metrics for opens, closes, samples sent/deduplicated, previews, curation revisions,
  duplicate/discard/promote decisions, and source retrieval failures.
- Document the lifecycle and troubleshooting in `docs/screenpipe.md`.
- Do not backfill historical ScreenPipe frames in the initial rollout.

## Test and acceptance plan

- Stable switches open and close exactly one observation. Sub-10-second passive transient
  switches are folded into the surrounding activity, while short interactions with input,
  media controls, title changes, or meaningful content changes are retained.
- Switching to a music player, changing playback, and immediately switching back creates
  a short music-player observation even when the whole interaction is under 10 seconds and
  occurs within another observation's two-minute sample cooldown.
- Six hours in one editor remains one observation while materially different code/context
  produces incremental samples.
- Static and idle screens do not create repeated content or previews.
- Sample retries, close retries, crashes, and restarts are idempotent and preserve ordering.
- Observations continue when audio forwarding is disabled.
- Each observation automatically transfers at most one preview, with one agent-requested
  alternative allowed.
- Audio sessions link by timestamp without mixing input and output semantics.
- Media dialogue cannot create personal facts; viewing progress and explicit reactions can.
- Incremental curation revisions cannot duplicate vault edits.
- Exact and semantic duplicates retain audit markers but no redundant preview or vault note.
- ScreenPipe and Immich promotion share content-hash deduplication and offline retry behavior.
- Agent outcomes cover no-op, discard, duplicate, text-only update, linked-note update,
  dedicated-note creation, and image promotion.
- Existing audio ingestion, conversation processing, bounded ScreenPipe queries, timeline
  retrieval, and Immich discovery remain functional.
- Live verification switches among several apps, spends an extended period coding in one
  window, plays system audio, and then switches away. The result must be one observation
  per meaningful context, incremental coding samples, correctly linked audio, sparse
  images, and conservative vault edits.

## Defaults

- Remote-open stability delay: 10 seconds; meaningful short interactions bypass it on
  close.
- Minimum interval between semantic samples in the same unchanged observation: 2 minutes;
  it never applies across app/window switches.
- Maximum active interval without a liveness sample: 15 minutes.
- Maximum OCR/accessibility excerpt per sample: 2,000 characters.
- Ranked frame candidates per observation: 3.
- Automatically transferred previews per observation: 1.
- Maximum previews including agent-requested fallback: 2.
- Maximum open-observation curation frequency: once per 15 minutes, plus final close.
- Immich correlation window: 30 minutes before or after the observation span.
- No fixed five-minute or 30-minute observation boundary.
- No initial historical backfill.
