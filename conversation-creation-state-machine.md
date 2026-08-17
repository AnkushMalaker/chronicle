# Conversation creation state machine

This is the current streaming path for HAVPE and other long-lived WebSocket sources.
Capture durability and semantic Conversation creation are deliberately separate state
machines.

## Non-negotiable invariants

1. Audio persistence never waits for or queries a Conversation.
2. A WebSocket/recovery attempt maps to one `AudioCaptureSession` and one Redis WAL.
3. Only deliberate intent or detected meaningful speech creates a Conversation.
4. The active Conversation pointer coordinates semantic jobs and plugins only; it never
   routes audio bytes.
5. Finalization attaches range claims to immutable capture chunks; it does not move
   them.

## Transport and fan-out

HAVPE sends one `audio-start` for a connection and then streams PCM until disconnect.
The relay forwards Wyoming events to the backend WebSocket. Session initialization
creates the Redis session state and Mongo `AudioCaptureSession`, then starts independent
consumers over one stream:

```text
audio:stream:{capture_session_id}
        |
        +-> streaming STT or windowed batch STT -> transcription results
        +-> audio persistence -> immutable Mongo capture chunks
        `-> wake-word detector -> wakeword:detections
```

The consumers do not own one another. In particular, an STT outage cannot make the
audio persistence consumer discard bytes.

## Capture-session state

```text
missing
   |
   | init_session + AudioCaptureSession.insert
   v
active
   |
   | flush producer buffer -> append terminal WAL marker -> mark finalizing
   v
finalizing
   |
   | persistence drains pending + new WAL entries, commits Mongo, then ACKs
   v
complete
```

Mongo commit precedes Redis `XACK`. A retry reclaims pending messages and looks up the
deterministic source-message identity, so a crash after commit and before ACK cannot
duplicate audio.

Each Mongo chunk stores `user_id`, `capture_source_id`, `capture_session_id`, `sequence`,
absolute `captured_at`, duration, codec data, and WAL provenance. It has no Conversation
foreign key or Conversation-relative coordinates.

## Detected-Conversation state

```text
listening_for_speech
   |
   | meaningful transcript gate (and optional enrolled-speaker check)
   v
materialize_detected_conversation
   |  deterministic segmentation_key
   |  set active_conversation_id in typed session state
   v
monitoring
   |  update live transcript; stop on inactivity, close request, session end, or cap
   v
finalizing semantic claim
   |  wait for persistence to reach claim end
   |  attach AudioRangeRef[] over existing chunks
   |  persist transcript artifact + Conversation transcript revision
   |  trim the claim, if configured
   v
speaker / summary / memory-policy / plugin jobs
   |
   | clear active semantic pointer; re-arm speech detection if capture is active
   v
listening_for_speech
```

The materializer can expose a live Conversation before persistence catches up, but the
Conversation is not a persistence prerequisite. At finalize, an empty unbacked semantic
shell is discarded; a real transcript is retained even if its audio claim could not be
attached, because losing speech evidence is worse than keeping an audio-less record.

## No-speech and batch fallback

If live segmentation is off or yields no transcript, the capture still persists. After
the capture (or a bounded off-mode compute window) completes, batch fallback:

1. claims the available capture ranges;
2. transcribes those ranges;
3. persists an immutable `TranscriptArtifact`;
4. runs the meaningful-speech gate; and
5. creates a detected Conversation only when the gate passes.

No-speech output remains queryable processing/capture evidence and creates no Recordings
row.

## Deliberate uploads and ScreenPipe

- File upload is deliberate: create a visible Conversation immediately, persist an
  idempotent finite capture, and attach its range.
- ScreenPipe is continuous evidence: persist the complete mixed window first, profile it
  with VAD, and create visible detected Conversations only for speech-bearing clipped
  ranges. Silence has an `AudioEvidenceSpan` but no Conversation.
- ScreenPipe Conversations skip per-Conversation vault memory; settled Timeline-day
  memory owns the continuous-capture memory unit.

## Wake word and plugins

Wake-word detection is a sibling stream consumer. The dispatcher may read
`active_conversation_id` from typed session state to star or request closure of the
semantic Conversation. With no active Conversation the request is rejected rather than
stored for a future, unrelated interval.

## Redis coordination

| State | Purpose |
|---|---|
| `audio:session:{id}` | Typed capture-session state, format, status, active semantic pointer |
| `audio:stream:{id}` | Raw-audio WAL shared by independent consumer groups |
| `transcription:results:{id}` | Live STT evidence consumed by detection/monitoring |
| `transcription:interim:{id}` | Ephemeral UI updates |
| `transcription:complete:{id}` | Provider completion signal |
| `speech_detection_job:{id}` | Single-flight detection job ownership |
| `open_conversation:session:{id}` | Open semantic-job bookkeeping |
| `session:signal:{id}` | Immediate finalize/close wake-up |
| `wakeword:detections` | Wake-word events for the dispatcher |

There is no `conversation:current:{id}` audio-routing key and no `always_persist`
Conversation. The only Conversation pointer is a semantic field inside typed session
state.

## Remaining seam

The models and range resolver support one Conversation claiming several capture
sessions. Automatically continuing an open detected Conversation across a WebSocket
reconnect is still follow-up orchestration; reconnect does not compromise raw capture
durability in the meantime.
