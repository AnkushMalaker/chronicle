# Audio pipeline architecture

Chronicle persists continuous audio independently of semantic Conversations. A
Conversation is a deliberate or speech-detected claim over durable audio, not the
container that makes capture possible. The authoritative decision is
[ADR-0001](adr/0001-separate-audio-capture-from-conversations.md); shared terminology is
in [CONTEXT.md](../CONTEXT.md).

## Layers

```text
device / upload / ScreenPipe
            |
            v
  technical capture session
            |
      Redis audio WAL
            |
            v
 AudioChunkDocument[]  <-------------------------------+
  immutable captured_at                                |
            |                                           |
            +--> VAD / STT / pyannote artifacts         |
            |                                           |
            +--> speech/deliberate materialization      |
                              |                         |
                              v                         |
                        Conversation                    |
                        AudioRangeRef[] -----------------+
                              |
                              v
                 ConversationTranscriptRevision
                              |
                    Recordings / Timeline / vault
```

The important Seam is `services/audio_claims.py`. Playback and semantic operations use
that Interface rather than querying audio by Conversation ID.

## Capture Module

### `AudioCaptureSession`

One technical ingest/recovery attempt. It records:

- user and stable `capture_source_id`;
- unique `capture_session_id`;
- origin (`streaming`, `upload`, `batch`, `screenpipe`, or `import`);
- time basis and absolute bounds;
- source stream/external-source provenance; and
- finite-import PCM digest for retry validation.

A reconnect creates a new capture session. It does not decide a semantic boundary.

### `AudioChunkDocument`

An approximately ten-second Opus document with:

- `user_id`, `capture_source_id`, `capture_session_id`, and `sequence`;
- immutable absolute `captured_at` and duration;
- audio format and compressed bytes;
- optional Redis WAL provenance and chunk-local VAD.

It has no `conversation_id`, `chunk_index`, `start_time`, or `end_time`. Those were
conversation-relative ownership fields and are intentionally absent.

The primary indexes are unique `(capture_session_id, sequence)`, unique WAL provenance
for replay, and `(user_id, capture_source_id, captured_at)` for wall-clock discovery.

## Streaming capture

`AudioStreamProducer.init_session` creates the Redis session/WAL and the Mongo
`AudioCaptureSession` before ingress jobs begin. Each accepted message carries capture
identity, not a Conversation owner.

```text
audio-start
  -> initialize Redis session and Mongo capture session
  -> start persistence and configured transcription consumers
  -> accept audio

audio packet
  -> XADD audio:stream:{capture_session_id}
  -> persistence consumer reads pending/new entries
  -> encode a 10-second Opus document
  -> majority+journal Mongo commit
  -> XACK the exact Redis message IDs
```

Every WAL audio entry carries both `captured_at` and its time basis. Device clients
that provide a capture timestamp use `recorded`; otherwise the producer uses the
server's `received` clock. Ten seconds is only a target document size: when the next
entry's timestamp differs from the buffered audio's expected end by more than 250 ms,
both the producer and persistence worker commit their current buffers early and begin
again at the new absolute timestamp. Neither layer concatenates samples across a
capture discontinuity. Consequently, playback suppression, reconnects, and other
missing-audio intervals remain explicit gaps between immutable audio documents and
later `AudioRangeRef` claims.

If Mongo succeeds and the worker dies before ACK, the replay finds the same document
through its unique WAL provenance and ACKs it without duplicating audio. See
[Raw audio durability](backend/audio-durability.md).

### Speech-driven materialization

Streaming transcription and the speech gate observe the capture concurrently. When
speech is detected, `materialize_detected_conversation` creates an idempotent detected
Conversation using a deterministic segmentation key. The Conversation may be visible
while its claim is still live; finalization waits for persisted audio to reach the
semantic end, then attaches an `AudioRangeRef`.

Capture continues before, during, and after that Conversation. Closing a Conversation
does not rotate, reparent, or interrupt the capture stream.

## Finite audio: uploads and imports

Finite PCM is written through `convert_audio_to_chunks`. A caller may supply a
deterministic capture-session ID. Retrying with the same ID:

1. validates user, source, format, digest, time basis, and absolute start;
2. validates already committed contiguous chunks;
3. resumes missing chunks; and
4. returns the same range and chunk IDs when already complete.

Reusing an ID for different bytes raises an error. It never silently overwrites or
duplicates evidence.

A deliberate file upload creates a visible processing Conversation and attaches the
returned claim. The one-time historical-backup Adapter reconstructs source PCM from
WAVs and invokes this same current Interface; it does not recreate the old chunk model.

## ScreenPipe continuous capture

ScreenPipe transport items are staging inputs, not recordings. The Adapter:

1. groups a bounded compute window (normally at most two hours);
2. mixes its source WAVs;
3. persists the entire mixed window as a deterministic capture before VAD;
4. profiles VAD/acoustic evidence;
5. records definitive silence as an `AudioEvidenceSpan` with an audio claim, without a
   Conversation;
6. finds quiet semantic cuts for speech-bearing audio;
7. clips claims from the already-persisted capture; and
8. idempotently materializes and queues detected Conversations for those claims.

Retries reuse the raw capture. A semantic-materialization failure cannot leak another
copy of its audio.

## Claim Module

`AudioRangeRef` contains ordered chunk IDs, one capture source, exact absolute bounds,
time basis, and capture-session provenance. Edge chunks may be clipped.

`services/audio_claims.py` provides the deep operations:

- resolve ranges to clipped presentation-time chunks;
- claim a capture window;
- apply ranges and synchronize Conversation summaries;
- clip, partition, and merge claims;
- locate a playable Conversation claim from absolute wall-clock time; and
- find references before any retention decision.

This concentrates coordinate math and Mongo details in one Module. Controllers,
Timeline, export, split/merge, trim, and maintenance tools should not reproduce it.

## STT, diarization, and revisions

`TranscriptArtifact` stores immutable provider evidence over audio ranges. Provider
utterance boundaries are preserved as evidence even when they are fragmented.

`DiarizationArtifact` stores pyannote neural speaker turns and configuration. Speaker
identification labels these turns afterwards; it does not replace segmentation.

Artifact coordinates have two complementary parts:

- `start_seconds` and `end_seconds` preserve the provider's gap-elided presentation
  clock across all ordered ranges; and
- `audio_spans` contains one absolute physical piece for every `AudioRangeRef` the item
  intersects.

The split representation is necessary because adjacent presentation ranges can have a
real wall-clock gap or can overlap (for example, concatenated channels). One absolute
start/end pair cannot represent a cross-range interval honestly and can become
backwards when the next range starts earlier in wall-clock time. STT point timestamps
may have equal offsets and a point span. Neural diarization turns must have positive
presentation duration. `services/audio_claims.py` owns this mapping so producers,
cutover tools, and maintenance code cannot implement incompatible endpoint math.

`ConversationTranscriptRevision` is the derived display projection. It records the
artifact IDs used, projected words/segments, provider provenance, and a deterministic
retry key. Re-diarization, editing, or a boundary change creates a new revision.

The Conversation embeds only provider source versions and its current derived read
model. Older speaker projections remain available as standalone revisions and are
removed from the embedded cache after archival. This prevents repeated whole-corpus
processing from breaching MongoDB's 16 MB document limit on long recordings.

Pyannote receives at most twenty minutes per neural segmentation call. Longer claims
use overlapping compute windows and reconcile speaker identities across seams. The
window is a peak-memory/failure bound, never a Conversation boundary. `min_speakers`
and `max_speakers` are automatic (`null`) by default and are passed only when explicitly
configured. Community-1's internal `min_duration_off` remains zero so its exclusive
timeline cannot bridge across another speaker. Chronicle reduces fragmentation later
with an event-aware same-speaker collar, after words have exactly one turn owner.

If pyannote returns no turns despite valid timed ASR words, Chronicle does not restore
the provider's utterance boundaries. It groups the immutable word clock only across
short silence gaps, runs voice identification on those neutral spans, and records the
projection and artifact as `word_timeline_fallback` with `pyannote_empty` provenance.
Corpus validation accepts that fallback only when every ASR word is owned exactly once;
it is never reported as a successful neural timeline.

## Semantic operations

- **Playback** resolves ordered claims and clips edge chunks precisely.
- **Split** partitions claims and derived transcript coordinates.
- **Merge** concatenates/merges claims without moving audio.
- **Silence trim** narrows claims and writes a new active transcript revision; it never
  creates a silence-remnant owner or edits chunks.
- **Timeline** stores the same stable audio claims; Conversation IDs are lineage and
  navigation only.

## Retention

Soft-deleting a Conversation removes a semantic claim. It does not delete raw capture.
One chunk may also support a Timeline episode, evidence span, transcript artifact,
diarization artifact, or another Conversation.

Conversation-level audio archival therefore returns a conflict, and automatic
low-speech cleanup is classification-only. A future capture-retention policy must be
explicit and must check all claimant collections before deleting bytes.

## Failure behavior

- Failure before Redis accepts streaming audio closes ingress; uncommitted transport
  bytes are not called durable.
- Failure after Redis acceptance leaves unread/pending WAL entries.
- Mongo failure causes RQ retry and no ACK.
- Finite capture retries validate identity and resume.
- Semantic materialization retries use a stable segmentation key.
- Artifact/revision retries use stable retry keys and reject changed output under the
  same key.

There is no alternate silent storage path or fallback Conversation owner.

## Key files

- `models/audio_capture.py`
- `models/audio_chunk.py`
- `models/conversation.py`
- `services/audio_stream/producer.py`
- `workers/audio_jobs.py`
- `services/audio_claims.py`
- `services/device_audio_ingest.py`
- `services/processing_artifacts.py`
- `workers/transcription_jobs.py`
- `workers/speaker_jobs.py`
