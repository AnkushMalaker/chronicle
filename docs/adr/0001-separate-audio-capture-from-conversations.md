# ADR-0001: Separate durable audio capture from conversations

- Status: **Accepted**
- Date: 2026-08-12
- Scope: audio persistence, transcription, diarization, conversation segmentation,
  playback, retention, and the Recordings page

## Context

Chronicle records audio continuously and creates Conversations as semantic claims over
useful speech. Before this decision, the implementation violated that model:

- every `AudioChunkDocument` requires a `conversation_id`;
- Redis audio entries must have an existing MongoDB Conversation owner;
- persistence fails closed when audio has no Conversation owner;
- transcript versions live only inside a Conversation; and
- split, merge, silence trim, playback, cleanup, and most processing utilities query
  audio through `conversation_id` and conversation-relative offsets.

Consequently, streaming capture created durable placeholder Conversations before
Chronicle knew whether a conversation occurred. ScreenPipe used hidden
`capture_evidence` Conversations for the same reason. This preserved audio, but made a
semantic object serve as a technical persistence container.

`AudioChunkDocument.captured_at` and Timeline audio ranges established the better
foundation: an audio document has its own immutable wall-clock identity, while a
semantic object can reference stable chunk IDs and exact absolute bounds.

## Decision

Chronicle separates the durable capture layer from the semantic Conversation
layer. Audio persistence, STT, VAD, and neural speaker segmentation must work without
creating a Conversation. A Conversation will be materialized only because the user
deliberately created one or because segmentation identified a speech-bearing interval
that should be user-visible.

### 1. Durable capture model

`AudioCaptureSession` is the document for technical ingest and recovery state. It is a
transport attempt, not a recording or semantic boundary. Reconnects may create new
capture sessions while remaining part of one logical continuous source.

An audio chunk's durable identity will be independent of every Conversation:

```python
class AudioChunkDocument(Document):
    user_id: str
    capture_source_id: str       # stable device/channel identity
    capture_session_id: str      # technical ingest/recovery attempt
    sequence: int                # order within the capture session
    captured_at: datetime        # immutable absolute UTC start
    duration: float

    audio_data: bytes
    sample_rate: int
    channels: int

    source_stream: str | None
    source_first_message_id: str | None
    source_last_message_id: str | None
    source_message_ids: list[str]
    vad: VADResult | None
```

Remove the following from the chunk model and persistence contract:

- `conversation_id`;
- conversation-relative `chunk_index`;
- conversation-relative `start_time`; and
- conversation-relative `end_time`.

`source_stream` and Redis message IDs remain write-ahead-log provenance. They are not
semantic recording identity.

Required indexes include:

- unique `(source_stream, source_first_message_id)` for idempotent WAL replay;
- unique `(capture_session_id, sequence)` for ordered ingest recovery; and
- `(user_id, capture_source_id, captured_at)` for absolute-range discovery.

### 2. Shared audio range claims

Promote the stable-reference design in `TimelineAudioRange` into a shared
`AudioRangeRef` used by Conversations, Timeline episodes, transcripts, diarization,
annotations, and playback:

```python
class AudioRangeRef(BaseModel):
    range_id: str
    capture_source_id: str
    chunk_ids: list[str]
    started_at: datetime
    ended_at: datetime
```

Each range is ordered and contiguous for one capture source. Gaps or parallel channels
are represented by separate ranges. `started_at` and `ended_at` may clip the first and
last ten-second chunks, so adjacent semantic claims can safely reference different
portions of the same edge chunk.

Chunk IDs and absolute bounds are authoritative. Capture-session identifiers are
provenance only.

### 3. Processing artifacts exist before conversations

Persist bounded processing output independently of Conversations:

- `TranscriptArtifact`: raw provider output, provider-relative words/utterances and
  their physical audio spans, provider/model provenance, one or more `AudioRangeRef`s,
  status, and a deterministic retry key.
- `DiarizationArtifact`: pyannote neural segmentation turns, speaker embeddings or
  labels, presentation offsets and physical audio spans, model/configuration
  provenance, one or more `AudioRangeRef`s, status, and a deterministic retry key.
- VAD remains chunk-local because it follows the immutable audio document.

An artifact has one gap-elided presentation clock across its ordered ranges. Every
word, utterance, and speaker turn stores `start_seconds`/`end_seconds` on that clock and
an ordered `audio_spans` list. One span identifies each physical `AudioRangeRef` piece
the interval intersects. This is required because concatenated ranges may be separated
or may overlap in absolute wall-clock time; mapping only the two endpoints can create a
false continuous interval or even an end timestamp before its start. STT may retain a
zero-duration provider point, but a neural diarization turn must have positive
presentation duration.

Provider utterance boundaries are retained as evidence but are not canonical semantic
segments. Smallest.ai's live fragments, for example, remain useful timestamped STT
evidence without forcing the same broken boundaries into the user transcript.

Pyannote's twenty-minute windows are bounded compute units only. Neighboring windows
may overlap and reconcile speaker identities across their seam; they do not create
audio-storage or Conversation boundaries.

### 4. Conversation materialization

A Conversation becomes a user-visible semantic object containing one or more
`AudioRangeRef` claims:

```python
class Conversation(Document):
    user_id: str
    audio_ranges: list[AudioRangeRef]
    started_at: datetime
    ended_at: datetime
    origin: Literal["deliberate", "detected"]
    active_transcript_revision_id: str | None
```

Creation rules:

- **Continuous capture:** store audio and processing artifacts continuously. Ambient,
  silent, and unclassified intervals do not create Conversations.
- **Detected conversation:** meaningful-speech segmentation creates a Conversation at
  speech onset, including configured pre-roll, and extends its range while speech
  continues. Finalization fixes the claim without moving chunks.
- **Deliberate recording or upload:** create a visible Conversation immediately in a
  processing state and claim the deliberately supplied audio. It appears on the
  Recordings page alongside other Conversations.

Materialization must be idempotent. A stable segmentation key based on origin, capture
source, first claimed audio message/chunk, and segmentation-policy revision prevents a
retry from producing duplicate Conversations.

### 5. User-facing transcript revisions

Create `ConversationTranscriptRevision` from the Conversation's range claims plus the
relevant transcript and diarization artifacts. Fusion aligns absolute STT words with
pyannote turns, then generates coherent speaker turns for display.

The stored processing artifacts remain immutable evidence. Editing, re-diarization,
or a changed Conversation boundary creates another derived revision; it does not
rewrite the raw STT or diarization output.

`Conversation.transcript_versions` is only a bounded read cache of provider sources and
the active derived projection. Once a displaced projection is verified in
`ConversationTranscriptRevision`, its embedded copy is removed. Immutable revision
history must not accumulate inside one Conversation document: long recordings would
otherwise eventually exceed MongoDB's 16 MB document limit.

API responses may expose seconds relative to the Conversation for the existing player
and editor, but relative time is a presentation transform, not stored audio identity.

### 6. Split, merge, trim, and playback operate on claims

- Split clips and partitions `audio_ranges` and derived transcript revisions.
- Merge unions ordered ranges and regenerates the derived transcript revision.
- Silence trim adjusts the semantic claim; it does not move chunks into a synthetic
  Conversation.
- Playback reconstructs the ordered ranges and clips their edge chunks.
- Timeline episodes and Conversations use the same shared range resolver.

These operations never rewrite `captured_at`, reassign chunks, or re-encode audio.

### 7. Deletion and retention are separate decisions

Deleting a Conversation removes or soft-deletes a semantic claim. It does not silently
delete the underlying continuous capture evidence, which may support another
Conversation, Timeline episode, annotation, or processing retry.

Raw-audio retention is a separate explicit policy over capture documents. Before
deleting a chunk, cleanup must prove that the retention policy permits deletion and
handle every live range claim that references it. Automatic raw-audio cleanup remains
disabled unless explicitly configured.

## Invariants

The implementation must maintain these invariants:

1. Successful Redis WAL append never depends on a Conversation existing.
2. Every Mongo audio chunk belongs to a user, capture source, and technical capture
   session, and has immutable absolute time.
3. No transcript or diarization result requires a Conversation foreign key.
4. A Conversation's playable audio is exactly the union of its ordered range claims.
5. Compute-window boundaries never become semantic boundaries implicitly.
6. Provider transcript segmentation never overrides neural speaker segmentation merely
   because it arrived first.
7. Retrying persistence, processing, or materialization is idempotent.
8. The default Recordings list contains real Conversations, not capture placeholders.

## Pipeline

```text
device/upload
    -> Redis audio WAL
    -> AudioCaptureSession + AudioChunkDocument[]
                          |-> VAD
                          |-> TranscriptArtifact[]
                          `-> DiarizationArtifact[]
                                      |
                              speech segmentation/fusion
                                      |
                     deliberate or detected Conversation
                          -> AudioRangeRef[]
                          -> ConversationTranscriptRevision[]
                          -> Recordings / Timeline / memory
```

## Cutover

Chronicle is under active development, so this decision deliberately does not add a
dual-read compatibility layer or preserve the placeholder-ownership schema.

The code cutover was applied as one vertical change:

1. add capture sessions, shared range claims, and independent processing artifacts;
2. change producer and persistence jobs to write capture coordinates rather than a
   Conversation owner;
3. change streaming and batch processing to write capture-referenced artifacts;
4. materialize detected and deliberate Conversations from ranges;
5. change playback, split/merge/trim, cleanup, Timeline, and speaker processing to use
   range claims;
6. remove `conversation:current`, placeholder creation/rotation, `always_persist`, and
   conversation-keyed audio reconstruction; and
7. back up the development corpus, reset the old audio/conversation data, and reingest
   it through the new path.

Items 1–7 are implemented for the development corpus. A self-contained post-cutover
archive was checksum-verified before artifact-coordinate repair, and the rebuilt corpus
contains no conversation-owned chunk fields. The model and artifact store accept
multi-session ranges and capture-only transcript/diarization artifacts; automatic
continuation of one live detected Conversation across a WebSocket reconnect, and
capture-first scheduling of the normal speaker job, remain follow-up orchestration
work. Neither requires restoring Conversation ownership to chunks.

No migration script or legacy-field fallback will be added unless separately approved.

## Consequences

Benefits:

- continuous recording no longer pollutes the Conversation domain;
- audio and speech survive segmentation or STT failures without placeholder records;
- arbitrary provider fragments and twenty-minute compute windows no longer dictate
  Conversation boundaries;
- split, merge, trim, and Timeline regeneration become non-destructive claim edits;
- deliberate recordings retain immediate, predictable Recordings-page visibility; and
- retries become easier to reason about because capture and semantic materialization
  have separate idempotency keys.

Costs:

- playback and processing APIs must accept range claims instead of one
  `conversation_id`;
- transcript/diarization artifacts add collections and lifecycle rules;
- cleanup must reason about references rather than Conversation ownership; and
- the current persistence and conversation lifecycle tests must be rewritten around
  capture durability and materialization invariants.

## Acceptance criteria

This ADR may move to **Implemented** after the corpus cutover and remaining orchestration
work are complete. Tests already prove capture persistence without a Conversation,
no-speech capture without a Recordings row, deliberate materialization, range clipping,
claim-only split/merge/trim, and insert-before-ACK replay idempotency. The remaining
acceptance cases are:

- one automatically detected Conversation spanning reconnect-created capture sessions
  plays as one ordered recording;
- the normal pyannote job can be scheduled directly from capture ranges before a
  Conversation exists; and
- the verified backup, reset, and full-corpus reingest complete without legacy fields or
  compatibility adapters. (Complete for the development corpus on 2026-08-13.)
