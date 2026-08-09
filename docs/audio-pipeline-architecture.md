# Audio Pipeline Architecture

How audio flows through Chronicle from capture to storage, including processing stages, Redis streams, data storage, the plugin system, and error handling.

## Overview

Chronicle's audio pipeline is built on:

- **Redis Streams**: Distributed message queues for audio chunks and transcription results
- **Background Tasks**: Async consumers that process streams independently
- **RQ Job Queue**: Orchestrates session-level and conversation-level workflows

**Key Insight**: Multiple workers independently consume the **same audio stream** using Redis Consumer Groups, enabling parallel processing (transcription + disk persistence) without duplication.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        AUDIO INPUT                              │
│  WebSocket (/ws) │ File Upload (/audio/upload) │ Google Drive  │
└────────────────────────────────┬────────────────────────────────┘
                                 ↓
                    ┌────────────────────────┐
                    │  AudioStreamProducer   │
                    │  - Chunk audio (0.25s) │
                    │  - Session metadata    │
                    └────────────┬───────────┘
                                 ↓
                    ┌────────────────────────────────┐
                    │ Redis WAL (Per Recording)      │
                    │ audio:stream:{session_id}      │
                    └─────┬──────────────────┬───────┘
                          ↓                  ↓
          ┌───────────────────────┐  ┌──────────────────────┐
          │ Transcription Consumer│  │ Audio Persistence    │
          │ (streaming or batch)  │  │ Consumer Group       │
          │                       │  │                      │
          │ → Deepgram WebSocket  │  │ → Writes WAV files   │
          │ → Batch buffering     │  │ → Monitors rotation  │
          │ → Publish results     │  │ → Stores file paths  │
          │ → TRIGGERS PLUGINS    │  │                      │
          └───────────┬───────────┘  └──────────┬───────────┘
                      ↓                          ↓
          ┌───────────────────────┐  ┌──────────────────────┐
          │ transcription:results │  │ Disk Storage         │
          │ :{session_id}         │  │ data/chunks/*.wav    │
          └───────────┬───────────┘  └──────────────────────┘
                      ↓
          ┌───────────────────────┐
          │ TranscriptionResults  │
          │ Aggregator            │
          └───────────┬───────────┘
                      ↓
          ┌───────────────────────┐
          │   RQ Job Pipeline     │
          ├───────────────────────┤
          │ speech_detection_job  │ ← Session-level
          │         ↓             │
          │ open_conversation_job │ ← Conversation-level
          │         ↓             │
          │ Post-Conversation:    │
          │ • speaker_recognition │
          │ • memory_extraction ──┤→ memory.processed plugin
          │ • title_generation    │
          │ • event_dispatch ─────┤→ conversation.complete plugin
          └───────────┬───────────┘
                      ↓
          ┌───────────────────────┐
          │   Final Storage       │
          ├───────────────────────┤
          │ MongoDB: conversations│
          │ Disk: WAV files       │
          │ FalkorDB: Memories +  │
          │   Knowledge Graph     │
          └───────────────────────┘
```

## Data Sources

| Source | Endpoint | Details |
|--------|----------|---------|
| **WebSocket Streaming** | `/ws?codec=pcm\|opus&token=xxx&device_name=xxx` | Wyoming Protocol (JSON lines + binary). Handlers: `handle_pcm_websocket()`, `handle_omi_websocket()`. JWT required. |
| **File Upload** | `POST /api/audio/upload` | Multiple WAV files (multipart). Admin only. Device ID: `{user_id_suffix}-upload` or custom. |
| **Google Drive** | `POST /api/audio/upload_audio_from_gdrive` | Downloads from Google Drive folder ID, enqueues for processing. |

**File**: `backends/advanced/src/advanced_omi_backend/routers/websocket_routes.py` (WS), `api_router.py` (upload)

## Redis Streams: The Central Pipeline

### Audio Stream

Key: `audio:stream:{session_id}`

- Recording-specific isolation (a reconnect cannot mutate an older capture)
- Fan-out: multiple consumer groups read the same stream
- Uncapped and unexpired until terminal consumer-group drain is proven
- Every audio entry carries its immutable `conversation_id` owner

### Session Metadata

Key: `audio:session:{session_id}` — Redis Hash, no TTL while active

Fields: `user_id`, `client_id`, `connection_id`, `stream_name`, `status` (`active` → `finalizing` → `complete`), `chunks_published`, `speech_detection_job_id`, `audio_persistence_job_id`, `websocket_connected`, `transcription_error`

### Other Redis Keys

| Key | Type | Purpose | TTL |
|-----|------|---------|-----|
| `transcription:results:{session_id}` | Stream | Final transcription results | Deleted on conversation end |
| `transcription:interim:{session_id}` | Pub/Sub | Real-time interim results for UI | Ephemeral |
| `transcription:complete:{session_id}` | String | Completion signal (`"1"` or `"error"`) | 5 min |
| `conversation:current:{session_id}` | String | Current conversation ID (signals WAV rotation) | 24 hours |
| `audio:file:{conversation_id}` | String | Audio file path on disk | 24 hours |
| `session:conversation_count:{session_id}` | Counter | Conversations in session | 1 hour |
| `speech_detection_job:{session_id}` | String | Job ID for cleanup | 1 hour after close |
| `system:event_log` | List | Plugin event audit log (capped at 1000) | None |

## Producer: AudioStreamProducer

**File**: `services/audio_stream/producer.py` — runs in `chronicle-backend` container

1. **`init_session()`**: Creates `audio:session:{session_id}` hash, initializes in-memory buffer
2. **`add_audio_chunk()`**: Buffers incoming audio and atomically checks `ACTIVE`, snapshots `conversation:current:{session_id}`, and appends a fixed 0.25s entry to `audio:stream:{session_id}`
3. **`finalize_session()`**: Appends the residual audio and END marker before advancing producer state

## Dual-Consumer Architecture

Redis Consumer Groups enable two independent consumers on the same audio stream.

### Consumer 1: Transcription

**A. Streaming** (`services/transcription/streaming_consumer.py`)
- Consumer group: `streaming-transcription`
- Opens persistent Deepgram WebSocket per stream, sends chunks immediately
- Publishes interim results via Pub/Sub, final results to `transcription:results:{session_id}` stream
- Triggers `transcript.streaming` plugin event on final results
- ACKs messages after processing

**B. Batch** (`services/audio_stream/consumer.py`)
- Consumer group: `{provider}_workers` (e.g., `deepgram_workers`, `parakeet_workers`)
- Buffers 30 chunks (~7.5s), batch transcribes, adjusts timestamps, publishes results
- ACKs after publishing; it never trims the shared raw WAL

### Consumer 2: Audio Persistence

**File**: `workers/audio_jobs.py` — `audio_streaming_persistence_job()`

- Consumer group: `audio_persistence`
- Encodes PCM to Opus and writes `audio_chunks` documents to MongoDB in real time
- Reads the immutable `conversation_id` stamped on each WAL entry
- Replays pending entries before reading new entries
- Commits a majority+journaled Mongo chunk before ACKing exact Redis message IDs
- Updates each owning conversation's chunk-count, duration, and compression metadata

Placeholder creation and assignment are owned by
`services/audio_stream/conversation_lifecycle.py`. The module serializes creation per
session, checks that the session is still active, and atomically publishes an assignment
only when no current owner exists. This prevents a conversation close immediately followed
by session finalization from creating an empty, permanently active placeholder.

### Fan-Out Visualization

```
audio:stream:user01-phone
    ├─ Consumer Group: "streaming-transcription"
    │  └─ streaming-worker → Deepgram WS → results stream + plugins
    ├─ Consumer Group: "deepgram_workers"
    │  └─ batch workers → Buffer(30) → API → results stream
    └─ Consumer Group: "audio_persistence"
       └─ persistence-worker → WAV file on disk
```

## Transcription Results Aggregator

**File**: `services/audio_stream/aggregator.py` — stateless, in-memory

- **`get_combined_results(session_id)`**: Reads all entries from results stream, combines text/segments/words. Streaming mode uses latest final result; batch mode combines sequentially.
- **`get_realtime_results(session_id, last_id)`**: Incremental polling for live UI updates.

## Job Queue Orchestration (RQ)

**File**: `controllers/queue_controller.py` — enqueued in `chronicle-backend`, executed in `rq-worker`

### Job Pipeline

```
Session Starts
    ↓
stream_speech_detection_job          ← Session-level, up to 24h
    ↓ (speech detected)
open_conversation_job                ← Conversation-level, up to 3h
    ↓ (conversation ends)
Post-Conversation Chain (RQ depends_on):
  [transcribe_full_audio_job]        ← File uploads only, RAISES on failure
  → recognize_speakers_job           ← 20 min timeout
  → memory_extraction_job            ← 15 min timeout
  → generate_title_summary_job       ← 5 min timeout
  → dispatch_conversation_complete   ← 2 min timeout
```

### Speech Detection Job

**File**: `workers/transcription_jobs.py` — `stream_speech_detection_job()`

Polls `TranscriptionResultsAggregator` at 1s intervals. Speech criteria: word count > 10, duration > 5s, confidence above threshold. When detected: creates conversation in MongoDB, enqueues `open_conversation_job`, exits (restarts after conversation ends). Checks `transcription_error` flag on each poll.

### Open Conversation Job

**File**: `workers/conversation_jobs.py` — `open_conversation_job()`

1. Creates conversation in MongoDB
2. Sets `conversation:current:{session_id}` → triggers WAV file rotation
3. Polls transcription updates (1s), dispatches `transcript.streaming` plugin events
4. Tracks inactivity (60s timeout). End conditions: disconnect, manual stop, inactivity, plugin close
5. Waits for transcription completion (30s max) and audio file path
6. Enqueues post-conversation pipeline
7. Calls `handle_end_of_conversation()` → conditionally clears its own assignment and,
   only if the session remains active, assigns the next always-persist placeholder before
   re-enqueuing speech detection

### Post-Conversation Jobs

| Job | What it does | On Failure |
|-----|-------------|------------|
| `transcribe_full_audio_job` | Batch transcribes full audio (file uploads only). Dispatches `transcript.batch` plugin event. | **Raises** → blocks entire chain |
| `recognize_speakers_job` | Sends audio + segments to speaker service, updates speaker labels | Returns dict → chain continues |
| `memory_extraction_job` | LLM extracts facts, stores in FalkorDB (Chronicle native) or OpenMemory. Dispatches `memory.processed` plugin event | Returns dict → chain continues |
| `generate_title_summary_job` | LLM generates title/summary, updates MongoDB | Returns dict → chain continues |
| `dispatch_conversation_complete_event_job` | Dispatches `conversation.complete` plugin event | Returns dict |

**Critical RQ behavior**: A raised exception marks a job "failed" and all dependent jobs stay **deferred forever**. This is why most post-conversation jobs return `{"success": False}` instead of raising.

### Session Restart

`handle_end_of_conversation()` (`utils/conversation_utils.py`): Deletes transcription results stream, increments conversation count, re-enqueues speech detection if WebSocket still connected.

## Data Storage

### MongoDB (`chronicle` database)

**`conversations` collection**:
```python
{
    "conversation_id": "uuid",
    "audio_uuid": "session_id",
    "user_id": ObjectId,
    "client_id": "user01-phone",
    "title": "Meeting notes",
    "summary": "...",
    "detailed_summary": "...",
    "transcript": "Full text",
    "audio_path": "1704067200000_user01-phone_convid.wav",
    "active_transcript_version": "v1",
    "transcript_versions": { "v1": { "text": "...", "segments": [...], "words": [...], "provider": "deepgram" } },
    "segments": [SpeakerSegment],  # mirrors active version
    "created_at": "...", "completed_at": "...",
    "end_reason": "user_stopped|inactivity_timeout|websocket_disconnect",
    "deleted": false
}
```
Indexes: `user_id`, `client_id`, `conversation_id` (unique)

**`audio_chunks` collection**: ~10-second Opus audio documents grouped operationally by
`conversation_id`. Capture/processing paths may create hidden provisional conversation
containers before a user-visible conversation exists.

Each document is ~10 seconds of Opus. Its `chunk_index`/`start_time`/`end_time` are
relative to whichever conversation currently owns it, and split, merge and silence
trimming all renumber them — they are a view, not identity.

`captured_at` is the identity: the absolute wall-clock start of that audio, written
once and never rewritten. It is what makes a conversation a **claim over an interval**
rather than a container, so audio can be re-bounded by moving a pointer instead of
rewriting every chunk, and trimmed audio keeps its provenance with no separate record.
The streaming path derives it from the Redis stream ID (already a millisecond
timestamp); uploads take it from the caller, which is how continuous capture anchors a
mixed window to the moment it was recorded.

Timeline episodes persist this semantic relationship in `audio_ranges`: each range
freezes stable Mongo chunk IDs plus exact absolute UTC bounds. Those references remain
valid when split, merge, or trim moves a chunk to a different operational conversation.
`related_conversation_ids` remains evidence and lineage context; it is not playback or
temporal truth. Chunks still carry a required `conversation_id` so existing processing,
reconstruction, and administration can group them, but changing that grouping no
longer changes what audio an episode claims.

#### Trimmed-silence lifecycle

Finalization runs VAD-driven silence trimming when enabled. With the defaults, it keeps
5 seconds around speech, moves only silent runs of at least 120 seconds, and does
nothing unless at least 60 seconds would be saved. Whole chunks are reassigned—never
re-encoded—to a new soft-deleted conversation titled `Trimmed silence`, with
`deletion_reason: silence_trim` and `derived_from` lineage. The surviving conversation
and its active transcript are packed onto a contiguous relative timeline; every chunk
on both sides keeps its original `captured_at`.

The remnant is recoverable soft-deleted data, not a permanent archive. Chronicle's
normal deleted-conversation purge also deletes its chunks once `retention_days` has
elapsed. Retention defaults to 30 days, but `backend.cleanup.auto_cleanup_enabled` is
false by default. An administrator may preview or trigger the purge through
`POST /api/admin/cleanup` (`dry_run=true` is the safe preview). Enabling automatic
cleanup explicitly opts into hard deletion after the configured window.

Audio written before the field existed is anchored by
`scripts/backfill_chunk_capture_time.py` (dry run by default). It resolves an anchor per
conversation — evidence span, then `derived_from` lineage for split/trim children whose
own `created_at` is the operation time, then `created_at` — and offsets each chunk by its
`start_time`. It deliberately **skips** audio recorded elsewhere and uploaded later
(`data_purpose: annotation`, the `-upload` device, `annotation_dataset`), because there
`created_at` is when the file arrived. A null anchor means "unknown" and can be fixed
later; a plausible-but-wrong one silently corrupts every future stitch, so the script
also withdraws anchors a previous run should not have written.

Once anchored, a recording's old bounds stop being load-bearing, so audio already stored
under the blind 30-minute cap can be re-bounded to what the current pipeline would have
produced. `scripts/reset_recording_bounds.py` (dry run by default) reassembles capture
windows from `captured_at`, then per window **merges** the pieces the cap severed,
**splits** at speech-derived seams, and **trims** the silence — three existing operations,
no re-encode, everything soft-deleted with lineage rather than destroyed. It reads the
speech profile from the chunks' stored VAD scores, so it decodes no audio. `--scope
fenced` touches only `capture_evidence`, which is excluded from memory; `screenpipe`
adds the promoted stretches, which are the recordings a user actually sees; `all` also
re-bounds live device capture, whose memories must then be rebuilt. A window is held
back rather than half-applied when a recording the scope may not touch sits inside it.

Measured over this deployment's ScreenPipe corpus: 231 recordings across 87 windows
became 165 recordings, and 93.6 h of stored audio became 52.4 h.

**Unanalyzed audio must be held back, not read as silent.** Audio nobody has run VAD
over produces no speech intervals, which is indistinguishable from silence unless the
caller checks — and reading it as silence is the worst available error, because the cut
planner then sees a uniformly quiet window and places its cut at exactly the target,
reinstating the blind 30-minute cut the tool exists to remove. That happened here to 18
windows / 17.5 h before the check existed, and it hid itself twice over: those windows
also reported as "carrying no speech at all", and the "no cut landed in speech" check
passed vacuously on them.

The measurement that missed it is worth remembering. In a Mongo *aggregation*, a
missing field does not compare equal to null, so `{$ne: ["$vad.scores", null]}` counts
chunks with no `vad` field at all as analyzed. Use an explicit size check instead:

```js
{$gt: [{$size: {$ifNull: ["$vad.scores", []]}}, 0]}
```

### An annotation keyed to a container does not survive the container

Everything Chronicle learns about a stretch of audio — that it is conversational, what
was said in it, who said it — is attached to a `Conversation`. But a conversation is a
*container*, and three routine operations replace the container while leaving the audio
exactly where it was: dedup of an ingest retry, merge/split when re-bounding, and the
silence trim. Each one is a small data loss at a different layer, and they are the same
bug three times:

| Annotation | What replacing the container does |
|---|---|
| an episode's `related_conversation_ids` | the citation points at a soft-deleted id, so promotion unhides nothing — 126 of 638 episodes here, including six generations of one real meeting |
| the `conversational` promotion itself | it is a flag on a whole recording, so carrying it across new bounds spreads it to every child that overlaps, and the next pass spreads it again — 17 → 23 recordings, 442 → 493 minutes over two passes, and "promoted" also means memory-eligible |
| `transcript_versions` | versions are keyed to the conversation, so a re-bound child starts from a fresh slice and the earlier versions stay behind on the dead parent |

Read-time resolution fixes the citation case without rewriting stored evidence:
`services/timeline/recording_refs.py` resolves a cited id to whatever is live now, by
`derived_into` lineage and — for dedup, which records no lineage because the surviving
copy never knew about the twin — by live audio covering the same span on the same
capture stream. It is deliberately stream-matched: the other node, or the other
direction of the same call, is different audio.

**Promotion carry-forward is disabled** (`Window.promoted` in
`scripts/reset_recording_bounds.py` is a documented no-op). A re-bound leaves every
child fenced, and visibility is re-derived afterwards from the episodes with
`scripts/repromote_conversational_episodes.py`, where it is computed from the agent's
bounds instead of inherited from whatever container last held it. Computed can shrink;
inherited can only grow.

None of this is the real fix. A conversation id is operational lineage, not temporal
identity — `captured_at` is the identity, so a durable annotation should be anchored to
an absolute time range over chunks. See [multimodal-memory.md](multimodal-memory.md):
an episode is the claim that a conversation spans a range of audio documents, and a
user-visible recording is best materialized from that claim rather than guessed at by a
silence heuristic underneath it.

### An operation that moves audio must preserve the audio's identity

`create_conversation` defaults every distinguishing field off, so each one has to be
inherited explicitly by split and merge or it is silently dropped — and the damage only
shows up somewhere else, long after the operation looked successful:

| Dropped | What breaks |
|---|---|
| `data_purpose`, `memory_excluded` | continuous capture becomes memory-eligible and ambient room audio walks into the vault |
| `external_source_type`, `external_source_id` | the recording vanishes from the timeline, which selects audio evidence by source type, and loses the capture direction that decides its media/speech role |
| `created_at` (split children) | a child stamped with the moment the split ran is filed on today, not the day it was recorded |

All three are covered by `tests/test_split_merge_fencing.py`. They matter far more at
the scale of a re-set than for a one-off admin split: the tool above performs hundreds
of merges and splits, so a laundered field becomes a corpus-wide fault.

### Disk Storage

Location: `backends/advanced/data/chunks/` (volume-mounted)
Format: `{timestamp_ms}_{client_id}_{conversation_id}.wav`
Created by `audio_streaming_persistence_job()`, read by post-conversation jobs. Manual cleanup only.

### Memory Storage

- **FalkorDB** (Chronicle native): Container `falkordb`, port 6379. Graph (ConvDoc/ConvChunk/ConvEntity + Entity/Relationship KG) with embedded vector + BM25 indexes. Hybrid search.
- **OpenMemory MCP**: Container `openmemory-mcp`, port 8765, cross-client storage (uses its own internal Qdrant).

Both written by `memory_extraction_job()`, read by `/api/memories/search`.

## Plugin System

**Framework**: `backends/advanced/src/advanced_omi_backend/plugins/` (base.py, router.py, events.py, services.py)
**Implementations**: `plugins/` at repo root

### Events

| Event | Emitted By | When |
|-------|-----------|------|
| `transcript.streaming` | Streaming consumer + open_conversation_job | Each final transcription result |
| `transcript.batch` | transcribe_full_audio_job | After batch transcription |
| `conversation.complete` | dispatch_conversation_complete_event_job | After all post-conversation jobs |
| `memory.processed` | memory_extraction_job | After memory extraction |
| `conversation.starred` | conversation_controller.toggle_star() | User stars/unstars via API |
| `button.single_press` / `button.double_press` | websocket_controller._handle_button_event() | OMI device button tap |
| `plugin_action` | PluginServices.call_plugin() | Cross-plugin call |

**Note**: `transcript.streaming` is dispatched from **two** sites — the streaming consumer (FastAPI process) and `open_conversation_job` (RQ worker process) — ensuring wake-word plugins react in real-time.

### Event Data

| Event | Key Fields in `PluginContext.data` |
|-------|-----------|
| `transcript.*` | `transcript`, `segment_id`, `conversation_id`, `segments`, `word_count` |
| `conversation.complete` | `conversation` (dict), `transcript`, `duration`, `conversation_id` |
| `memory.processed` | `memories` (list), `conversation` (dict), `memory_count` |
| `conversation.starred` | `conversation_id`, `starred` (bool), `starred_at`, `title` |
| `button.*` | `state`, `timestamp`, `audio_uuid`, `session_id`, `client_id` |

### Discovery and Loading

On startup (`app_factory.py`, Phase 4):
1. `discover_plugins()` scans `plugins/` directory for subdirectories with `plugin.py`
2. Imports module, finds `BasePlugin` subclass by introspection
3. Three-layer config merge: `plugins/{id}/config.yml` (defaults) → `plugins/{id}/.env` (secrets) → `config/plugins.yml` (orchestration)
4. Instantiates plugin, calls `register_prompts()`, `register_plugin()`, `initialize()`
5. Builds inverted index: `_plugins_by_event[event] → [plugin_ids]`

**RQ Workers**: `ensure_plugin_router()` handles lazy re-initialization in worker processes.
**Hot Reload**: `reload_plugins()` purges `sys.modules`, builds new router, atomically swaps global.

### Dispatch Flow

`PluginRouter.dispatch_event()`:
1. Lookup plugins by event (O(1) index)
2. For each enabled plugin: check condition → build context → call handler
3. On exception: log with traceback, continue to next plugin (never propagates)
4. Log event to `system:event_log` Redis list
5. `should_continue=False` stops the chain; exceptions do not

### Condition Types

| Type | Behavior |
|------|----------|
| `always` | Always executes |
| `wake_word` | Checks if transcript **starts with** any `wake_words`, extracts command after wake word |
| `keyword_anywhere` | Checks if keyword appears **anywhere**, extracts remaining text as command |
| `conditional` | Reserved for future use (currently always executes) |

Button and starred events bypass all transcript-based conditions.

### Plugin Services

```python
await context.services.close_conversation(session_id, reason)   # Trigger post-processing
await context.services.star_conversation(session_id)             # Star/unstar
result = await context.services.call_plugin("homeassistant", "toggle_lights", data)  # Cross-plugin
```

`close_conversation()`: Checks `conversation:current:{session_id}` in Redis, signals `open_conversation_job` to end.
`call_plugin()`: Direct call bypassing router dispatch.

### ASR Keyword Hints

Plugin router collects `wake_words`/`keywords` from enabled plugins via `get_asr_keywords()`, injected as recognition hints into Deepgram (`keyterm`) and VibeVoice (`context_info`).

### Configuration

```yaml
# config/plugins.yml (orchestration, committed)
plugins:
  my_plugin:
    enabled: true
    events: [transcript.streaming, conversation.complete]
    condition: { type: wake_word, wake_words: ["hey chronicle"] }
    api_url: ${MY_API_URL}

# plugins/{id}/config.yml (non-secret defaults, committed)
# plugins/{id}/.env (secrets, gitignored)
```

### Button Event Flow

```
OMI Device (BLE button) → friend-lite-sdk (parse_button_event)
  → BLE Client (Wyoming protocol: {"type": "button-event", ...})
  → Backend (_handle_button_event) → dispatch_event to plugins
  → Plugin (on_button_event) → can close_conversation/call_plugin
```

### Existing Plugins

| Plugin | Events | Purpose |
|--------|--------|---------|
| `email_summarizer` | `conversation.complete` | Emails conversation summaries |
| `homeassistant` | `plugin_action` | Smart home control via cross-plugin calls |
| `test_event` | `conversation.complete` | Test/debug event logging |
| `test_button_actions` | `button.single_press`, `button.double_press` | Button → close conversation, star, call plugin |

## Failure Handling

### Per-Component Behavior

| Component | On Failure | Net Effect |
|-----------|-----------|------------|
| **Streaming transcription** (Deepgram WS) | Sets `transcription_error` in session hash, re-raises. No retry. | Speech detection exits on next poll. User must reconnect. |
| **Batch transcription** (API) | Logged, messages NOT ACKed. Dead consumer cleanup (30s) eventually ACKs and discards. | Failed chunks silently lost. |
| **Speech detection job** | Checks `transcription_error` each poll. 60s no-activity watchdog. 2h max runtime. | No conversation created. No automatic restart. |
| **Conversation job** | `try/finally` always calls `handle_end_of_conversation(end_reason="error")`. | Session always recovers; failed conversation may be marked deleted. |
| **Audio persistence** | Buffer NOT cleared on write failure (can retry next chunk). | Audio partially lost on persistent failures; logged but not surfaced. |
| **WebSocket disconnect** | `cleanup_client_state()` → `finalize_session()` → 60s TTL on stream. Does NOT cancel speech detection. | Orderly shutdown. In-flight processing completes. |
| **Redis connection** | No circuit breaker. Unhandled `ConnectionError` → RQ marks job failed. | Dependent jobs stay deferred forever. Manual recovery needed. |

### Post-Conversation Job Failures

| Job | On Failure | Chain Impact |
|-----|-----------|-------------|
| `transcribe_full_audio_job` | **Raises** | All downstream deferred **forever** |
| `recognize_speakers_job` | Returns dict | Chain continues without speaker labels |
| `memory_extraction_job` | Returns dict | Chain continues without memories |
| `generate_title_summary_job` | Returns dict | Chain continues without title |

### Conversation Job `try/finally` Pattern

```python
end_of_conversation_handled = False
try:
    # ... conversation phases ...
    end_of_conversation_handled = True
    return await handle_end_of_conversation(...)
finally:
    if not end_of_conversation_handled:
        await handle_end_of_conversation(..., end_reason="error")
```

### Dead Consumer Cleanup

- **Automatic**: `cleanup_dead_consumers()` — 30s idle threshold → XCLAIM + XACK (discards, no reprocess) + DELCONSUMER
- **Manual**: `cleanup_stuck_stream_workers()` endpoint — 5 min idle threshold, same pattern
- **Zombie RQ jobs**: `check_job_alive()` called each iteration of long-running jobs; exits if job missing from Redis

### Edge Cases

- **No meaningful speech**: `transcribe_full_audio_job` marks conversation `deleted=True`, `end_reason="no_meaningful_speech"` (word count < 10, duration < 5s)
- **Audio file not ready**: 30s timeout → conversation marked deleted with `end_reason="audio_file_not_ready"`
- **Stream trimming**: MAXLEN 25,000 on audio streams; results streams deleted after conversation ends
- **Session timeout**: 24h max → graceful exit, cleanup

### Job Timeouts

| Job | Queue | Timeout |
|-----|-------|---------|
| `speech_detection_job` | `transcription_queue` | 24 hours |
| `audio_persistence_job` | `audio_queue` | 24 hours |
| `recognize_speakers_job` | `default_queue` | 20 min |
| `memory_extraction_job` | `memory_queue` | 15 min |
| `generate_title_summary_job` | `default_queue` | 5 min |

On timeout, RQ kills the job. Dependent deferred jobs stay deferred forever.

### BackgroundTaskManager

**File**: `task_manager.py` — tracks all async tasks in the FastAPI process.

- `track_task()` / `_task_done()`: Register, detect completion/error, cap completed at 1,000
- `_periodic_cleanup()`: Every 30s, cancels tasks exceeding timeout
- `shutdown()`: Cancels all, waits 30s
- `cancel_tasks_for_client()`: On disconnect, cancels non-processing tasks (preserves `transcription_chunk`, `memory`, `cropping`)

### What the Pipeline Does NOT Do

- **No retry logic**: Failures log-and-continue or raise/return-failure. No backoff, no dead letter queues.
- **No circuit breakers**: External service outages cause immediate failures.
- **No cascade job cancellation**: Failed/timed-out RQ jobs leave dependents deferred forever.
- **No automatic session recovery**: Failed speech detection → session dead until reconnect.
