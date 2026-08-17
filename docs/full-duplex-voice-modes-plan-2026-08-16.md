# Full-Duplex Chronicle Voice Modes

**Status:** Revised proposed implementation plan; implementation requires phase gates

**Date:** 2026-08-16

**First acceptance workflow:** Swiggy Instamart order mode on the Chronicle phone app

**Initial platform scope:** iOS, then Android API 31+

## Executive decision

Build full duplex as a new, versioned phone capability on top of Chronicle's existing
Wyoming connection and capture evidence model. Do not replace streaming STT, raw audio
persistence, interaction modes, or semantic Conversations.

The feature consists of four coordinated changes:

1. A native phone audio engine owns microphone capture and Chronicle playback during an
   interactive voice session.
2. A low-latency, non-authoritative audio fan-out feeds a turn coordinator before the
   existing producer has accumulated a 250 ms durable chunk.
3. A Redis-backed response coordinator gives every output a generation and makes stale
   playback, LLM, TTS, and tool results suppressible.
4. Interaction modes receive only committed turns and use an explicit side-effect
   fence when work can no longer be cancelled safely.

Swiggy is the first acceptance workflow, not a special case in the audio runtime. The
runtime owns sessions, turn boundaries, routing, cancellation, and playback. The plugin
owns phase-specific turn policy, shopping language, cart state, and effect safety.

If the current route cannot prove safe simultaneous capture and playback, Chronicle
uses a visible half-duplex state. It must never silently pretend barge-in is available.

## Review verdict and corrections to the original proposal

The original direction was sound, but it was not implementable safely without the
following corrections.

This review treats the observed failures in
[`swiggy-voice-attempt-diagnosis-2026-08-15.md`](./swiggy-voice-attempt-diagnosis-2026-08-15.md)
as the baseline regression set, not as isolated symptoms.

| Original gap | Decision in this plan |
| --- | --- |
| The existing 250 ms Redis audio chunks were treated as sufficiently low latency. | Preserve that durable WAL, but fan 20-100 ms interactive frames to a bounded ephemeral turn path before buffering. |
| `client_id` was treated as a safe playback target. | Every interactive downlink is bound to both the active capture session and a voice-session epoch. Old WebSockets and old epochs must reject it. |
| A same-client reconnect could retain a mode automatically. | Rebinding requires an authenticated, single-use resume token. A fresh recording without that token ends the old interaction. |
| Transcript hashes were expected to resolve wake/streaming races. | Route by overlapping audio intervals and a single routing claim before either interpretation can dispatch side effects. |
| `stop-audio` and JavaScript playback were treated as an adequate cancellation path. | One native scheduler owns playback and acknowledges `started`, `done`, `cancelled`, or `failed`; generation checks exist on the phone and backend. |
| The worker was assumed to be cancellable. | Add a cooperative cancellation scope and an explicit irreversible-effect fence. Some external calls may finish, but their stale results cannot speak or mutate twice. |
| A route change could replay the same response automatically. | A route change cancels the response. Repeating speech is a new response and only happens if the mode deliberately requests it after readiness. |
| Android compatibility was left open-ended. | Full duplex initially requires API 31+ communication-device APIs. Do not add deprecated route compatibility branches without a separate decision. |
| Slow multi-item dictation was an acceptance case, but Swiggy currently permits exactly one tool call per turn. | Add a bounded multi-item read-only intent; search concurrently, consolidate the reply, and serialize all mutations. |
| “Do not change audio persistence” conflicted with capture-profile seams. | Do not redesign persistence. Add the minimum required capture epoch/profile provenance, and ensure a chunk never spans an audio-engine transition. No migration script. |

## Goals

- Combine streaming transcript fragments into one intentional user turn.
- Let a user interrupt Chronicle while Chronicle is speaking.
- Stop obsolete playback quickly and prevent obsolete generation from reaching the
  device.
- Preserve the first word of an interruption with VAD pre-roll.
- Keep far-field ambient capture outside an interactive mode.
- Give modes exclusive, deterministic routing for audio they have claimed.
- Preserve checkout's at-most-once behavior and make interruption safe around every
  external side effect.
- Make limited routes, reconnects, background restrictions, and failures visible.
- Produce enough telemetry to diagnose false barge-in, latency, stale output, and
  background-task failures without recording raw audio in traces.

## Non-goals

- Replacing Wyoming framing, streaming STT, the durable raw-audio WAL, or audio chunk
  storage.
- Changing Conversation materialization, timeline claims, memory extraction, or
  reconciliation. Voice sessions and turns are operational objects, not Conversations.
- Introducing LiveKit, WebRTC, or the Hugging Face speech-to-speech runtime.
- Streaming partial TTS in the first release. Version 1 schedules one complete WAV per
  response; `sequence` is reserved for a later chunked response protocol.
- Filler speech, elevator music, speculative spoken answers, or model-driven checkout.
- Starting an interactive session while the app is already suspended in the
  background. Continuing an already-active session is a later, separately gated
  capability.
- Supporting full duplex through remote speakers, Cast-like routes, or an unverified
  Bluetooth media route.
- Supporting deprecated Android audio-route APIs as a compatibility layer.

## Invariants

1. `AudioChunkDocument.captured_at` remains the wall-clock identity of captured audio.
2. An `audio_session_id` identifies a technical ingest attempt, not a semantic
   conversation or a durable interaction identity.
3. A `voice_session_id` identifies one interaction binding to an authenticated phone
   socket. It is never reused after reconnect or a later mode activation. Native engine
   and route rebuilds within that binding increment `capture_epoch` instead.
4. A `turn_id` identifies one user utterance. Reopening increments its revision rather
   than creating a second plugin input.
5. A `response_id` identifies one logical output. Incrementing the client-wide
   generation makes all work and playback carrying an older generation stale.
6. Only a committed routing claim may reach a plugin. The same audio interval cannot be
   dispatched to both ordinary Hermes handling and an interaction mode.
7. Only a committed turn may cause a plugin mutation.
8. The phone has at most one speaking response. A newer speech response supersedes the
   current and queued speech response.
9. Crossing an irreversible-effect fence disables task cancellation, not stale-output
   suppression.
10. No capture chunk spans two native capture profiles or capture epochs.
11. A literal `Unknown Speaker N` remains conversation-local and is irrelevant to voice
    session identity.

## Identity and ownership

| Identifier | Owner | Lifetime | May survive reconnect? | Purpose |
| --- | --- | --- | --- | --- |
| `audio_session_id` | WebSocket ingest | One audio-start/audio-stop attempt | No | Durable capture/WAL and STT identity |
| `interaction_id` | Interaction mode store | One plugin interaction | Only through validated resume | Plugin state and phase |
| `voice_session_id` | Voice-session coordinator | One interactive authenticated-socket binding | No; a resume creates a new voice session | Duplex capability and downlink target |
| `capture_epoch` | Native audio engine | One uninterrupted audio configuration | No | Detect gaps, resets, and capture-profile seams |
| `turn_id` + `revision` | Turn coordinator | One utterance plus reopen revisions | Only while the interaction resumes | Endpointing and transcript assembly |
| `response_id` + `generation` | Response coordinator | One output and its validity epoch | No stale generation survives | Cancellation and ordered playback |

The response coordinator may be indexed by `(user_id, client_id)` for serialization,
but a downlink is deliverable only when its `audio_session_id` and `voice_session_id`
match the subscriber's active binding.

## Current baseline that the implementation must account for

- Phone capture currently uses `expo-audio-studio` at 16 kHz mono PCM16 and sends
  roughly 100 ms frames.
- The backend producer combines incoming PCM into fixed 250 ms Redis WAL entries. That
  cadence is appropriate for durable capture but too coarse to be the only barge-in
  signal for a 700 ms end-to-end target.
- `audioPlayback.ts` creates independent `expo-audio` players, and
  `audioPlaybackGate.ts` drops microphone buffers while playback is active. Both
  behaviors must leave the interactive path.
- Device downlinks are currently published by `client_id`, so a stale and a fresh
  socket can briefly receive the same output.
- Streaming final fragments currently enter `InteractionIngress` independently.
- The wakeword consumer sees Redis message IDs but does not retain audio interval bounds
  in its wake event. Cross-source deduplication therefore falls back to normalized text
  hashes.
- `InteractionModeWorker` serializes work with a Redis lock but has no task cancellation
  scope. Delivery calls TTS and device playback directly.
- The wakeword executor also publishes TTS and tones directly and uses a timed mute;
  the phone independently applies a playback capture gate.
- A mode's active pointer is keyed by user/client and can outlive an audio reconnect.
- Swiggy's ordinal selection currently matches an isolated word such as `two` inside an
  unrelated sentence, and its current Luna contract expects exactly one tool call per
  spoken turn.

These are implementation inputs, not reasons to preserve the unsafe behavior.

### Primary code surface

The design should remain concentrated in these existing boundaries:

| Area | Existing code to change or wrap |
| --- | --- |
| Phone native audio | `app/modules/chronicle-mic-control/` (replaced by `chronicle-duplex-audio`) |
| Phone capture/transport | `app/src/hooks/usePhoneAudioRecorder.ts`, `useAudioStreamingOrchestrator.ts`, and `useAudioStreamer.ts` |
| Phone playback | `app/src/utils/audioPlayback.ts` and `audioPlaybackGate.ts` |
| WebSocket ingest/downlink binding | `backends/advanced/src/advanced_omi_backend/controllers/websocket_controller.py` |
| Durable producer and low-latency branch | `services/audio_stream/producer.py` and a new bounded voice-frame publisher |
| Streaming transcript intervals | `services/transcription/streaming_consumer.py` |
| Wake/turn detection | `extras/wakeword-service/detector.py` and `consumer.py` |
| Mode contract/routing/execution | `services/interaction_modes/contracts.py`, `ingress.py`, `store.py`, and `processor.py` |
| Existing device TTS paths to centralize | `services/device_audio.py` and `services/wakeword/executor.py` |
| First plugin | `plugins/swiggy_instamart/plugin.py` |

Prefer a new response-coordination service and a reusable turn-segmentation module over
growing `websocket_controller.py` or the Swiggy plugin into additional orchestrators.
Existing interaction and Swiggy tests should be extended, while the WebSocket and
background-worker entry points need new direct tests.

For version 1, run the active-session turn consumer inside the existing wakeword
service as a separate module, Redis consumer group, and bounded task. Reuse the loaded
Silero/Smart Turn models behind an explicit inference scheduler; do not make active-turn
frames pass through the per-wakeword interpreters. Keep the frame/event contract clean
enough to split this into a dedicated process later if measured loop lag or model
contention requires it.

## Target flow

```text
native 20-100 ms PCM frames
          |
          +--> bounded voice-frame fan-out --> VAD / turn coordinator
          |                                    |
          |                                    +--> turn revisions + endpoint
          |
          +--> existing 250 ms durable WAL --> persistence + streaming STT
                                                   |
turn interval + transcript watermark --------------+
          |
          v
routing claim --> committed InteractionInput --> mode worker
                                                   |
                                    cancellable LLM/read work
                                                   |
                                      irreversible-effect fence
                                                   |
                                      response coordinator
                                                   |
                                 targeted response.audio event
                                                   |
                                  one native playback scheduler
                                                   |
                              playback ACK / barge-in cancellation
```

## Release scope and capability negotiation

Version 1 is available only to the React Native phone app after it advertises
`voice_duplex_protocol: 1` in `audio-start`. Other device classes retain their current
noninteractive playback path; they do not receive an emulated full-duplex promise.

Backend support is deployed first with the feature flag off. The native app is then
deployed. Full duplex is enabled only for allow-listed user/device pairs after the
backend has observed a compatible capability.

There is one version 1 schema. Do not accept aliases, legacy field names, or partially
compatible variants. An unsupported or malformed version fails explicitly and cannot
activate full duplex.

## Native phone audio engine

### Module and ownership

Replace the narrow iOS-only `chronicle-mic-control` module with a cross-platform local
Expo module named `chronicle-duplex-audio`. Move the existing iOS microphone-mode and
far-field controls into it so there is one owner of native audio state.

During an interactive voice session, the module owns both:

- microphone capture, converted to Chronicle's 16 kHz mono PCM16 wire format; and
- Chronicle WAV playback, including queue replacement, stop, flush, and native
  playback acknowledgements.

JavaScript passes control envelopes and encoded response data across the Expo boundary,
but it must not create a second audio player or alter the platform audio session while
the native engine owns it. Outside an interaction, `expo-audio-studio` remains the
far-field recorder.

The module exposes one serialized state machine:

```text
idle -> far_field -> switching -> duplex_full
                              \-> duplex_isolated
                              \-> duplex_half
```

All transitions run under one native mutex and increment `capture_epoch`. A callback
from an older epoch is discarded. Start/stop operations are idempotent for the current
requested epoch and fail loudly for conflicting requests.

`duplex_full` means simultaneous capture and playback with verified voice processing.
`duplex_isolated` means simultaneous capture and playback through acoustically isolated
headphones. `duplex_half` means capture is intentionally gated while Chronicle speaks.

### iOS implementation

- Configure `AVAudioSession` as `.playAndRecord` with `.voiceChat` while interactive.
- Use one `AVAudioEngine` input path and `AVAudioPlayerNode` output path.
- Enable voice processing before starting the engine. Read back success; a requested
  setting is not proof of an operational echo canceller.
- Observe the actual hardware input format and resample natively to 16 kHz PCM16.
  Never assume that the route itself runs at 16 kHz.
- Route built-in playback to speaker as requested and use HFP for bidirectional
  Bluetooth communication. Classify eligibility from both the actual input and output,
  not a display name such as “AirPods.”
- Treat wired/USB/headphone output as isolated only when the selected input remains
  stable and the output is not acoustically exposed.
- Handle interruption began/ended, route changes, audio-services reset, engine
  configuration changes, and media-services loss. Each rebuild creates a new epoch and
  cancels current playback.
- Restore the prior Chronicle far-field session deliberately on exit; do not rely on
  implicit `AVAudioSession` deactivation behavior.
- Preserve existing Control Center microphone-mode diagnostics. Do not recommend Wide
  Spectrum during an interactive session.

References:

- [Apple: `AVAudioSession.Mode.voiceChat`](https://developer.apple.com/documentation/AVFAudio/AVAudioSession/Mode-swift.struct/voiceChat)
- [Apple: `setVoiceProcessingEnabled`](<https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:)>)
- [Apple: What's New in AVAudioEngine](https://developer.apple.com/videos/play/wwdc2019/510/)

### Android implementation

- Gate native full duplex to Android API 31+ and use `setCommunicationDevice`; do not
  add deprecated routing branches in this work.
- Acquire audio focus for voice communication and set `MODE_IN_COMMUNICATION` only for
  the active engine epoch. Restore the previous mode, focus, and selected device on
  exit, failure, or interruption.
- Capture with `AudioRecord` and `MediaRecorder.AudioSource.VOICE_COMMUNICATION`.
- Request and enable `AcousticEchoCanceler` and `NoiseSuppressor` for the actual record
  session. Report `available`, `requested`, `enabled`, and any initialization error
  separately; `enabled=true` is still subject to the runtime acoustic-health check.
- Play through one `AudioTrack` configured for voice communication. `cancel` performs
  pause, flush, and release/safe reuse on the audio thread, then reports the monotonic
  stop timestamp.
- Resample the actual input rate to 16 kHz natively.
- Observe communication-device changes, audio focus loss, recorder errors, dead audio
  objects, and Bluetooth disconnects. Each rebuild increments the epoch.
- Full duplex on built-in speaker requires working AEC and a healthy runtime route.
  Headphones may use `duplex_isolated` without AEC. Remote or uncertain output uses
  `duplex_half`.

References:

- [Android: communication-device routing](https://developer.android.com/reference/android/media/AudioManager#setCommunicationDevice(android.media.AudioDeviceInfo))
- [Android: `MediaRecorder.AudioSource`](https://developer.android.com/reference/android/media/MediaRecorder.AudioSource)
- [Android: `AcousticEchoCanceler`](https://developer.android.com/reference/android/media/audiofx/AcousticEchoCanceler)

### Capture-profile seam

Changing between far-field and an interactive engine is an evidence boundary:

1. Stop accepting frames from the old capture epoch.
2. Send `audio-stop` for the old ingest session and flush any partial producer buffer.
3. Start the new native engine and a fresh `audio-start` ingest session on the same
   authenticated WebSocket where possible.
4. Persist required `capture_epoch` and `capture_profile` provenance on newly written
   chunks. Profiles are `far_field`, `duplex_aec`, `duplex_isolated`, and
   `half_duplex`.
5. Persist which platform effects were actually enabled. Duplex audio may contain
   AEC/NS/AGC-processed microphone evidence rather than an untouched microphone signal;
   the provenance must make that explicit.
6. Record the transition gap, old/new profile, routes, and monotonic/capture timestamps
   as an operational event.

Do not move, trim, or rewrite captured chunks. Do not create a migration for historical
development data. New writers and readers change together.

## Voice-session protocol

### Event directions

Server to phone:

- `audio-session.started`
- `voice-session.start`
- `voice-session.stop`
- `response.audio`
- `response.cancel`

Phone to server:

- `voice-session.ready`
- `voice-session.capabilities-changed`
- `voice-session.resume`
- `voice-session.stopped`
- `response.playback`

The existing Wyoming JSONL-plus-binary framing remains. These are additional validated
control events, not a second socket protocol.

`audio-session.started` is the WebSocket acknowledgment for `audio-start`; it returns
the backend-assigned `audio_session_id` and echoes `capture_epoch`. This acknowledgment
is required because that backend identity does not currently reach the phone. The phone
must receive it before sending `voice-session.ready` or accepting a targeted response.

Every voice-session event with an active ingest binding carries this common envelope:

```json
{
  "protocol": 1,
  "event_id": "uuid",
  "client_id": "...",
  "audio_session_id": "...",
  "voice_session_id": "...",
  "capture_epoch": 12,
  "sent_at": "UTC timestamp"
}
```

`audio-session.started` establishes `audio_session_id`. A reconnecting
`voice-session.resume` necessarily has no current audio session; it instead carries the
previous voice-session ID, previous capture epoch, single-use resume token, and last
observed response generation. After validation, the normal start/readiness handshake
establishes the new IDs. No other event may omit its current audio and voice session.

The backend derives and verifies `user_id` from the authenticated socket; it never
trusts one sent by the client. Unknown fields and invalid state transitions are rejected
with a structured protocol error. Set explicit limits on control-message size, WAV
size, response duration, queue depth, and acknowledgment time.

Deduplicate control messages by `event_id` for a bounded retry window. A duplicate must
return the prior result without repeating a transition. The protocol schemas have
Pydantic and TypeScript definitions plus shared golden JSON fixtures so direction,
required fields, enums, and rejection behavior cannot drift independently.

### Start and readiness

1. A routing claim confirms mode activation.
2. The backend creates a `voice_session_id`, a short-lived opaque resume token, and
   publishes `voice-session.start` to the currently bound capture session.
3. The app transitions through `switching`, ends the far-field ingest session, starts
   the duplex engine and a fresh ingest session, waits for `audio-session.started`, and
   returns `voice-session.ready` with that assigned audio session.
4. Readiness includes actual input/output route classes, native sample rate, engine
   epoch, AEC/NS requested/available/enabled values, selected mode, and any fallback
   reason.
5. The backend binds the new audio session to the voice session atomically. Only that
   socket may receive its response events.
6. Plugin work may begin during the transition, but spoken output remains queued until
   readiness. If readiness does not arrive in two seconds, the app/backend enters an
   explicit `duplex_half` state or fails activation visibly; it never sends unguarded
   speakerphone audio.

The initial reply is not allowed to bypass this handshake.

Persist the server-side voice session as a small Redis state machine:

```text
starting -----> ready_full | ready_isolated | ready_half
ready_* ------> reconfiguring -> ready_*
ready_* ------> reconnecting -> ended
any state --------------------> ended
```

Only one non-ended voice session may be bound to `(user_id, client_id)`. State changes,
socket binding, capture-session binding, and resume-token rotation are atomic. Route or
native-engine reconfiguration keeps the voice-session ID, creates a new capture epoch
and ingest session, and repeats `audio-session.started` plus readiness before output can
resume.

### Route change, interruption, and stop

On a native route change, interruption, or engine error:

1. The native engine cancels playback locally before waiting for the network and
   increments `capture_epoch`.
2. The app reports `voice-session.capabilities-changed` with a bounded reason and enters
   `switching`; the backend increments response generation and enters `reconfiguring`.
3. If capture configuration changed, end the prior ingest session and start a new one.
4. Repeat readiness only after the actual route and effects are stable.
5. Do not replay the cancelled response. A new mode response can be offered after
   readiness if it is still useful.

On `voice-session.stop`, the app terminally cancels playback, ends the interactive
ingest session, restores the far-field recorder in a fresh capture epoch/session, and
returns `voice-session.stopped`. The acknowledgment reports restoration success or a
bounded capture failure. Stop is idempotent. The backend does not describe the phone as
far-field/listening again until restoration succeeds; a disconnect still finalizes the
old ingest and voice sessions through normal cleanup.

### Downlink isolation

The Redis downlink can remain indexed by `client_id`, but each subscriber filters every
interactive event against its server-side active binding before forwarding it. The app
performs the same check before scheduling audio. This double check closes the interval
where an old and a new socket are both subscribed.

Do not route merely because `client_id` matches. A mismatch is counted as a stale
downlink drop and acknowledged only in telemetry, never to the old sender as successful
playback.

### Reconnect and resume

- On loss of the active socket, the interaction enters `reconnecting` for at most 15
  seconds. The app cancels native playback as soon as it observes socket loss; the
  backend independently increments generation and suppresses new speech output.
- Resume requires the same authenticated user/client, the last voice-session resume
  token, and the expected previous epoch. The token is single-use, stored hashed,
  expires with the grace period, and rotates on success.
- A successful resume creates a new `voice_session_id`, capture epoch, and audio ingest
  session, then repeats capability readiness. The interaction may survive; the old
  voice session never does.
- A fresh `audio-start` without the resume proof is not a resume. End the old mode with
  `audio_disconnect` so a later unrelated recording cannot feed it.
- A response that was playing at disconnect is terminally cancelled. Do not replay it
  automatically. A mode may deliberately create a new current-state response after
  successful readiness.

## Low-latency audio and turn coordination

### Ephemeral frame fan-out

The current 250 ms audio WAL remains the durable source for persistence and streaming
STT. For a full-duplex voice session, the WebSocket ingest path additionally publishes
each incoming 20-100 ms native frame before producer buffering to a bounded ephemeral
stream such as `voice:frames:{voice_session_id}`.

Each frame carries:

- voice session and capture epoch;
- monotonically increasing frame sequence;
- absolute UTC `captured_at`, monotonic offset within the epoch, and time basis;
- sample rate, channel count, and sample count; and
- the associated current audio ingest session.

Interactive `audio-chunk` events use `time_basis: captured` with the native monotonic
sequence/timestamp mapping. Establish the wall-clock/monotonic mapping once at epoch
start; a clock discontinuity starts a new epoch. Interactive chunks must not inherit the
current phone direct-stream path's `received` time basis merely because they are not
durable-spool replays.

The stream is non-authoritative and aggressively bounded. It may be lost across a
worker failure; durable capture may not. A sequence gap, epoch change, or timestamp
discontinuity cancels the provisional turn, records a metric, and waits for a clean
boundary. It must never guess across missing audio.

Use bounded queues and drop/cancel the provisional turn rather than allowing Redis or
the event loop to accumulate unbounded PCM. Monitor the fan-out separately from the
durable producer so a voice-turn slowdown cannot block capture persistence.

The voice-session coordinator publishes lifecycle control for the active-turn consumer;
the consumer does not discover sessions by scanning keys. It accepts frames only for a
currently ready binding, ignores duplicate sequences, and clears in-memory VAD state on
end or epoch change. A worker crash cancels provisional state and resumes at a clean
boundary; only committed turn records are durable.

### Turn segmenter

Extract the reusable VAD/Smart Turn endpoint logic from the wakeword detector into a
`TurnSegmenter`; do not duplicate model semantics in the backend. For version 1, run a
distinct active-voice-session consumer in the wakeword service so the existing
wakeword consumer group and durable capture behavior remain independent.

Turn state is:

```text
open -> soft_ended -> reopened(revision + 1) -> soft_ended
                  \-> committed
                  \-> cancelled
```

- Open on VAD onset and attach at least 500 ms of bounded pre-roll.
- Soft-end only after speech-to-silence and a Smart Turn decision.
- Reopening preserves `turn_id`, increments `revision`, and invalidates every derived
  transcript, endpoint decision, and routing reservation for the previous revision.
- Only `committed` turns reach `InteractionIngress`.
- Apply a seven-second maximum reopen window measured from the first soft end.
- Apply a 60-second maximum open turn and a bounded transcript/token size. On the hard
  limit, commit a clearly marked endpoint or fail the turn according to mode policy;
  never retain an infinite turn.
- Epoch changes, frame gaps, interaction end, and resume failure cancel an open turn.

Default policies:

| Policy | Complete grace | Incomplete grace | Intended use |
| --- | ---: | ---: | --- |
| `conversational` | 800 ms | 2 s | Short commands, confirmations, selections |
| `dictation` | 2.5 s | 4 s | Lists and slower item entry |

`InteractionModeDefinition` exposes a `TurnPolicy`; the audio runtime must not inspect
Swiggy phrases or phases. The mode can switch policy after a committed result. Immediate
commit phrases are normalized, explicit policy declarations such as `that's all` and
`done adding items`, not hard-coded audio-runtime behavior.

### Transcript assembly and watermark

Streaming STT continues to produce provider fragments. Extend stored and emitted
fragment metadata so it retains the audio session plus Redis message/sample bounds
already known by the consumer. Word timestamps remain session-relative and must not be
confused with wall-clock capture time.

For each soft-ended turn:

1. Select final fragments whose audio intervals overlap the turn interval.
2. Wait for the streaming STT watermark to pass the turn end plus a bounded settle
   delay.
3. Assemble and normalize those fragments once for the current turn revision.
4. If the watermark times out or an interval is missing, submit the exact durable audio
   range to the normal transcription provider as a bounded batch fallback.
5. If fallback fails, mark the turn failed and ask the user to repeat. Never dispatch a
   known partial transcript silently.

Provider reconnect offsets and wall-clock capture timestamps need targeted tests; they
are different coordinate systems.

### Cross-source routing arbitration

Replace normalized-text deduplication for voice-mode routing with an audio-interval
claim. Both wake and streaming candidates carry `audio_session_id`, capture epoch,
start/end message or sample bounds, and a stable candidate ID.

A single routing arbiter joins overlapping candidates and makes one atomic decision:

- an already-active mode has first refusal for committed turns in its interaction;
- a confirmed registered mode activation wins over ordinary Hermes interpretation for
  the same audio episode; and
- once an episode is committed to a route, no second route may dispatch it.

The wake detector publishes a provisional interval reservation as soon as it recognizes
an acoustic activation, before it has captured the complete command. A streaming
partial that plausibly matches a registered activation can create the same kind of
reservation. Voice-origin ordinary input has a short 150 ms reorder window in which to
observe a reservation; non-voice input does not. An overlapping reservation can hold
dispatch for at most 2.5 seconds while activation evidence completes. These values are
initial tuning parameters, not buried constants.

The wakeword consumer must propagate the Redis message/sample interval it currently
observes. If interval evidence is incomplete, the arbiter fails closed for mode
activation rather than relying on a text hash and risking two side effects.

## Response coordination

### State and storage

All interaction replies, Hermes replies, tones, and future backchannels enter one
response coordinator instead of calling `speak_on_device` directly.

```text
queued -> synthesizing -> ready -> offered -> playing -> done
                      \-> cancelled
                      \-> failed
```

Redis stores the current generation and response state for `(user_id, client_id)`. The
generation is a client-wide monotonic output epoch, not a counter local to a response.
An atomic `INCR` on a new committed user turn, explicit replacement, or cancellation is
the only operation that supersedes speech. Every asynchronous step checks
`(voice_session_id, response_id, generation)` before and after awaiting LLM, tool, TTS,
Redis publication, or playback acknowledgment.

Voice-session, response, resume-token, and event-deduplication keys have explicit TTLs.
Terminal response/deduplication records must outlive the maximum queue redelivery and
late-downlink window; active keys refresh only on valid state transitions, not arbitrary
client traffic. No failed session may leave an immortal active pointer.

Every response contains:

- `audio_session_id`, `voice_session_id`, `turn_id`, and turn revision;
- `response_id`, `generation`, and `sequence`;
- `kind` (`speech`, `tone`, or a future explicitly defined kind);
- `barge_in_allowed`;
- media type, sample rate, byte length, and duration; and
- trace/causation IDs without secrets.

Version 1 sends a complete WAV as `sequence: 0` in the Wyoming binary payload rather
than JSON base64. The phone has one current speech item and at most one pending speech
item. A new speech response atomically cancels both older items. Tones never overlap
speech and do not preempt it; a stale tone is dropped.

The current wearable/Opus path may adapt a committed response at its final transport
edge, but it cannot bypass generation and terminal-state accounting.

### Native playback acknowledgments

The native scheduler, not JavaScript intent, emits:

- `started` with the actual monotonic render-start timestamp;
- `done` after the final scheduled sample renders;
- `cancelled` after the audio queue is flushed, with the stop timestamp; or
- `failed` with a bounded error code.

An offer that receives no `started` acknowledgment within its timeout becomes `failed`.
Publishing to Redis or writing bytes to the socket is not playback success. Duplicate
offers and acknowledgments are idempotent by response ID and generation.

Initial safety limits are a 60-second response, a 16 MiB WAV payload, one pending speech
item, a two-second start-ACK timeout, and a one-second cancel-ACK timeout. Keep them
configurable and reject over-limit output before publication. A missing cancel ACK
marks the route unhealthy and forces session reconfiguration or half duplex.

### Barge-in

While a full or isolated duplex response is actually `playing`:

1. Require 300 ms of sustained VAD speech after any AEC warm-up.
2. Atomically increment the response generation immediately; do not wait for STT or a
   Smart Turn decision.
3. Publish `response.cancel` to the bound voice session.
4. Flush native playback and record its acknowledgment timestamp.
5. Open or reopen the user turn using retained pre-roll.
6. Signal cooperative cancellation to pre-effect LLM, TTS, and read-only tool work.
7. Suppress every result from the stale generation, even when its underlying call
   cannot be interrupted.

The same sustained-speech signal supersedes a response that is still queued,
synthesizing, ready, or offered, even though there is no playback to stop. This lets a
user revise a request while Chronicle is thinking and prevents the pending answer from
speaking over the newer turn. Work beyond an effect fence follows the serialization
rules below.

Measure barge-in latency from the frame timestamp of speech onset on the phone's
monotonic capture clock to the native playback-stop acknowledgment on the same clock.
Do not use backend receipt time for the primary metric.

Playback-period transcripts remain provisional until the turn commits. Assistant-text
similarity is useful for echo diagnostics and route-health scoring, but it must never be
the sole reason to discard speech: a user is allowed to repeat Chronicle's words.

### Cancellation and irreversible effects

Add a cooperative `CancellationScope` to the interaction execution context and a task
registry keyed by interaction, turn revision, response ID, and generation. The Redis
generation remains authoritative across worker restarts; in-memory task cancellation is
only an optimization.

Pre-fence work must not hold the interaction lock indefinitely. Network clients must be
async and cancellation-aware where possible. An uncancellable read-only call runs
behind a hard timeout; the worker may stop awaiting it, release sequencing, and discard
its eventual result by generation. No detached task may update plugin state. Post-fence
mutations keep serialization until their terminal/reconcile checkpoint, and newer turns
remain queued with a visible `using_tool` state.

Classify work explicitly:

| Work | Before effect fence | After effect fence |
| --- | --- | --- |
| LLM planning | Cancel if possible; always drop stale result | Not applicable |
| Read-only MCP/tool call | Cancel if possible; always drop stale result | Not applicable |
| Idempotent cart replacement | May retry with the same operation key | Finish/reconcile; never apply a different stale intent |
| Checkout or non-idempotent external action | Do not start without confirmed intent and durable checkpoint | Never replay automatically; finish/reconcile and suppress stale speech |

The effect fence is a durable checkpoint written immediately before the irreversible
call. Once crossed, interruption does not kill the task. The worker completes or records
an unknown outcome, reconciles plugin state, acknowledges the input only at a terminal
or durable checkpoint, and processes the newer turn afterward.

Exactly-once must not be claimed for an external system that offers no idempotency key.
Checkout remains at-most-once: an unknown outcome is surfaced for reconciliation and is
never retried from Redis redelivery.

## Acoustic route health and fallback

Readiness reports configuration; runtime behavior determines health.

- Built-in speaker is eligible for `duplex_full` only when voice processing/AEC is
  requested successfully and the runtime route is healthy.
- Acoustically isolated headphones are eligible for `duplex_isolated` even when AEC is
  unavailable.
- Remote, high-latency, acoustically exposed, or unknown routes use `duplex_half`.
- Allow an empirically measured warm-up at the start of the first playback. During
  warm-up the app captures for AEC but cannot declare barge-in.
- Two assistant-correlated false VAD interruptions in a rolling 60-second session
  window mark the route unhealthy for that voice session. This initial threshold must
  remain configurable and be tuned from device testing.
- AEC initialization failure immediately selects half duplex. Never leave open
  speakerphone capture running under a `full` label.

Half duplex gates capture only while native playback is actually rendering, plus a
measured short tail. The UI shows `Barge-in unavailable on this audio route`, and the
backend records the precise fallback reason. Route health resets on a new voice session;
it is not used as a permanent device fingerprint.

## Interaction-mode and Swiggy changes

### Generic mode contract

Extend the mode contract with:

- current `TurnPolicy`;
- immediate-commit phrases for that phase;
- effect classification for each action; and
- an optional post-resume prompt policy.

Interactive speech is bargeable whenever the negotiated route supports it. A plugin
cannot disable interruption to protect a mutation; the effect fence provides that
safety. The response coordinator derives `barge_in_allowed` from route state and output
kind, with tones and half-duplex playback not claiming barge-in.

Modes receive only committed turns. A mode cannot publish audio directly. It returns a
structured response request to the response coordinator. Phase changes and policy
changes are checkpointed together so Redis redelivery cannot apply one without the
other.

### Swiggy safety work before duplex rollout

1. Replace broad ordinal scanning. Accept explicit phrases such as `option two` or
   `number two`, and accept bare `two` only when the entire normalized turn is an
   expected selection. `The two different modes` must never select a product.
2. Preserve the deterministic `confirm order` boundary. An LLM cannot infer or invoke
   checkout from a shopping utterance.
3. Preserve full-cart replacement as an idempotent, checkpointed operation.
4. Preserve checkout's pre-call `checkout_in_progress` checkpoint and unknown-outcome
   reconciliation.
5. Change the one-tool-per-turn Luna contract to allow one bounded read-only
   `collect_items` intent containing 1-5 structured `{query, quantity, notes}` items.
   Execute searches concurrently with a semaphore of three, consolidate the candidates
   into one response, and keep selection/cart mutations serialized.
6. Reject a list longer than the bound with a spoken clarification; do not silently
   truncate it.

Swiggy uses `dictation` while collecting item lists and `conversational` for location,
candidate selection, cart review, and checkout confirmation.

## User-visible application states

The app exposes these server-driven states:

- `listening`
- `thinking`
- `using_tool`
- `speaking`
- `interrupted`
- `reconnecting`
- `limited_audio_route`

State events are generation-bound so a stale worker cannot move the UI backward from a
newer turn. `limited_audio_route` includes a user-safe reason and persists for the voice
session. Do not expose raw native error strings.

Foreground operation is the launch gate. Existing background-audio declarations or an
Android foreground service do not prove reliable duplex behavior. Continuing a live
voice session while backgrounded or locked is enabled per platform only after physical
tests for capture continuity, playback, route changes, interruption, and OS indicators.
If the operating system suspends the handshake, the app exits or visibly falls back; it
does not claim full duplex.

## Observability and operational safety

Propagate trace context through audio-session changes, turn revisions, route claims,
plugin work, effect fences, TTS, downlink, native playback, and interruption.

Record:

- voice/capture session transitions and gap duration;
- frame fan-out queue depth, drops, sequence gaps, and consumer lag;
- turn revisions, endpoint reason, STT watermark wait, and batch fallback;
- route class and AEC/NS requested/available/enabled/healthy status;
- speech-onset-to-native-stop latency;
- false barge-in, reopen, and half-duplex fallback counts;
- generation increments and stale LLM/tool/TTS/downlink/playback drops;
- playback offer, start, completion, cancellation, failure, and ACK timeout;
- native assistant-playback intervals mapped onto the capture clock, keyed by response
  ID/generation for diagnosis rather than treated as user speech;
- routing reservation latency and losing candidates;
- effect-fence crossings, redeliveries, and unknown external outcomes;
- event-loop lag and CPU/memory impact for backend and turn workers; and
- plugin/MCP/TTS latency.

Use bounded enums and non-identifying route classes. Do not put raw audio, resume
tokens, API credentials, or unredacted shopping transcripts into metrics. Langfuse text
capture follows the existing privacy configuration rather than being enabled by this
feature.

Background components need direct health signals. A healthy process is insufficient:
the turn consumer and response coordinator expose last-consumed IDs, lag, error counts,
and fresh-success timestamps through service health/system events.

Feature flags and kill switches:

- `VOICE_TURN_COORDINATOR_ENABLED`
- `VOICE_RESPONSE_COORDINATOR_ENABLED`
- `PHONE_DUPLEX_IOS_ENABLED`
- `PHONE_DUPLEX_ANDROID_ENABLED`
- per-user/client canary allow-list
- runtime force-half-duplex switch

The exact configuration mechanism should follow Chronicle's existing config model; the
names above define required independent controls, not necessarily final environment
variable names.

## Implementation sequence and phase gates

### Phase 0A: Prove native acoustic feasibility

Before committing to the production protocol implementation, build a narrow native
diagnostic harness that simultaneously captures processed microphone PCM and renders a
known WAV through the intended graph. It is not a second product audio path and should
be deleted or retained only as a test target after the production module exists.

Deliver for iOS first:

- built-in speaker and one HFP/headphone route;
- actual input/output formats and requested/enabled effect reporting;
- native render start/stop timestamps and immediate queue flush;
- captured fixtures for no-user TTS and real speech over TTS; and
- interruption, route-change, and engine-rebuild behavior.

Exit gate:

- On a physical iPhone, assistant playback does not create a usable speech transcript
  in ten no-user trials, real overtalk remains intelligible in at least nine of ten
  trials, and native stop/flush is deterministic. If this fails, revise the audio graph
  or narrow the route matrix before building the backend around an unproven assumption.

Run the equivalent Pixel/Samsung feasibility gate before Phase 4, without blocking the
iOS/backend phases on Android OEM work.

### Phase 0: Lock contracts and remove known Swiggy hazards

Deliver:

- an ADR for session identity, protocol state, route arbitration, and effect fences;
- Pydantic/TypeScript identity-event schemas, golden fixtures, and protocol validators;
- routing interval metadata on wake and streaming results;
- strict Swiggy ordinal handling;
- bounded multi-item read-only intent;
- response and effect classification in the plugin contract; and
- deterministic state-machine model tests.

Exit gate:

- The current half-duplex app completes the fake Swiggy flow without duplicate route,
  ordinal mis-selection, or checkout replay under Redis redelivery.

### Phase 1: Response coordinator on the current audio path

Deliver:

- Redis generation/state store;
- one backend delivery entry point for replies and tones;
- stale checks around TTS and device publication;
- playback status events and server timeout handling; and
- a phase-scoped adapter for the current phone playback path, behind the coordinator;
  remove it when Phase 3 cuts the phone to the version 1 native scheduler rather than
  maintaining two phone protocols.

Exit gate:

- Repeated replies cannot overlap, every output reaches one terminal state, and a stale
  response cannot play after cancellation or reconnect.

### Phase 2: Turn and routing coordinator in half duplex

Deliver:

- ephemeral frame fan-out;
- reusable `TurnSegmenter` and active-session consumer;
- transcript interval/watermark assembly and batch fallback;
- audio-interval routing arbiter; and
- `TurnPolicy` integration with interaction modes.

Exit gate:

- Slow item dictation creates one committed turn and one consolidated response; wake
  and streaming candidates from the same audio can produce only one route.

### Phase 3: iOS native engine and barge-in

Deliver:

- `chronicle-duplex-audio` iOS engine;
- capture-profile seams and protocol readiness;
- single native response scheduler;
- native cancellation acknowledgment;
- route health and half-duplex fallback;
- cooperative cancellation/effect-fence integration; and
- removal of JavaScript capture gating and timed backend TTS muting for sessions that
  have acknowledged `duplex_full` or `duplex_isolated`; half-duplex gating moves into
  the native scheduler and follows actual render acknowledgments.

Exit gate:

- iOS automated, native, physical, and acoustic criteria below pass with the canary
  flag disabled by default.

### Phase 4: Android API 31+ native engine

Deliver the equivalent Android engine, communication routing, focus restoration,
effects reporting, instrumentation tests, and Pixel/Samsung physical validation.

Exit gate:

- Android meets the same acceptance criteria or explicitly documents a tighter
  supported route matrix. It may not weaken the global safety threshold silently.

### Phase 5: Canary and live Swiggy acceptance

1. Deploy/restart only the affected Chronicle services after checking the live host and
   its worktree according to repository deployment guidance.
2. Build new native iOS/Android clients; an Expo update alone is insufficient.
3. Publish an internal/TestFlight build with flags off.
4. Enable one fake-MCP canary user/device and inspect traces and system health.
5. Run the complete fake flow repeatedly, including interruptions and injected
   failures.
6. Run one authorized live Swiggy attempt through UPI QR generation without payment.
7. Expand by platform and route class, not by a global percentage alone.

Rollback disables native full duplex and/or the new turn coordinator independently.
Rollback must leave response generation fencing enabled once the delivery path depends
on it.

## Verification strategy

### Unit and model-based tests

- Every legal and illegal native engine transition, including delayed callbacks from an
  old epoch.
- Turn open/soft-end/reopen/commit/cancel and hard limits under a fake monotonic clock.
- Frame duplication, reordering, loss, epoch change, timestamp discontinuity, and queue
  overflow.
- STT fragments before/after the watermark, provider reconnect offsets, missing ranges,
  and batch fallback failure.
- Overlapping wake/streaming intervals and routing reservation expiry.
- Atomic generation increments, duplicate events, out-of-order acknowledgments, and
  stale response rejection.
- Response replacement rules for speech and tones.
- Cancellation before and after every effect fence.
- Redis redelivery during cart replacement, checkout start, unknown checkout outcome,
  and terminal completion.
- Resume token mismatch, reuse, expiry, client mismatch, and successful rotation.
- Strict Swiggy ordinal parsing and bounded multi-item collection.

Use property/state-machine tests for the central invariants: at most one committed route
per audio episode, at most one playing response, monotonic generations, and no replay
after a non-idempotent fence.

### Registered entry-point tests

Tests must invoke production entry points, not helpers alone:

- the real WebSocket handlers for `audio-start`, duplex control events, audio frames,
  disconnect, and resume;
- the registered streaming and wakeword consumers with fake Redis/provider boundaries;
- the actual interaction worker handler, dependency lookup, lock, checkpoint, and ACK
  path;
- the response coordinator publication/subscription and socket filter; and
- the scheduled/background health reporting path.

Assert fresh job status, system events, or metrics after each background failure. A
still-running worker is not a passing signal.

### Native and app tests

- XCTest for iOS session activation, route/interruption/reset handling, epoch rejection,
  resampling, scheduler replacement, and stop acknowledgment.
- Android instrumentation tests for focus/mode/device restoration, recorder/effect
  setup, route changes, dead audio objects, scheduler replacement, and stop
  acknowledgment.
- React Native tests proving JavaScript never starts an `expo-audio` player or applies
  `audioPlaybackGate` while the duplex module owns audio.
- App lifecycle tests for foreground/background/locked transitions, with unsupported
  states producing an explicit exit or fallback.

### Failure injection

- Route change and Bluetooth disconnect during playback.
- AEC unavailable, AEC creation failure, and runtime false-echo threshold.
- WebSocket loss before readiness, while listening, during TTS, and beyond resume grace.
- Turn-worker and response-worker restart at every state transition.
- Redis duplicate delivery and delayed pub/sub messages.
- LLM, read-only MCP, cart mutation, checkout, and TTS timeout/cancellation refusal.
- Missing, duplicate, late, and contradictory native playback acknowledgments.
- Event-loop saturation and a slow voice-frame consumer.

### Physical-device matrix

- Current supported iPhone with built-in speaker/mic.
- iPhone with AirPods/HFP, wired headphones, and route change during playback.
- At least one current Pixel and one current Samsung on API 31+ with speakerphone.
- Android Bluetooth communication audio, wired headphones, and unavailable AEC.
- Ordinary and high speaker volume, quiet and moderate room noise, phone at near and
  normal speaking distance.
- Foreground first. Background and locked-screen scenarios are recorded separately and
  do not block foreground launch unless the app falsely reports capability.

## Acceptance criteria

Automated/replay acoustic suite:

- At least 500 no-user-speech TTS playback trials across representative impulse/noise
  fixtures produce zero committed user turns, zero user-facing Conversations, and no
  more than one provisional false barge-in. No plugin input may result from assistant
  echo.
- At least 100 injected real interruptions per supported route class stop playback in
  700 ms p95 from known speech onset, including the 300 ms confirmation window.
- The opening lexical item is retained in at least 95% of intelligible interruption
  trials; failures are reviewed against VAD pre-roll and STT alignment.
- A newer generation produces zero stale native playback starts in cancellation,
  reconnect, route-change, and worker-restart tests.

Physical smoke suite, per device/route combination:

- Twenty no-user-speech replies at ordinary/high volume produce zero false committed
  turns and zero false interruptions.
- Twenty deliberate interruptions retain the opening word in at least 19 and meet the
  700 ms target in at least 19. Raw individual timings are retained for p95 aggregation;
  a small sample is not used to claim a statistically strong p95 alone.
- A route change never overlaps players, leaks stale audio, or silently remains labeled
  full duplex after losing AEC/isolation.
- Headphone barge-in works without JavaScript capture suppression.

Functional Swiggy suite:

- Activation through both wake and streaming evidence yields exactly one mode start.
- Bangalore `Home`, slow entry of 2-5 items, product selection, cart review, explicit
  checkout confirmation, and UPI QR generation complete against the fake MCP without
  payment.
- `The two different modes` does not select option two.
- Interrupting read-only searches leaves only the latest consolidated response.
- Interrupting cart replacement applies the intended operation once.
- Interrupting checkout cannot replay it; timeout yields a visible unknown/reconcile
  state.

Performance and reliability:

- Durable audio persistence and streaming STT have no missing or reordered capture
  evidence attributable to the ephemeral fan-out.
- Voice-frame queues remain bounded, and overload cancels only provisional turn work.
- Backend and worker event-loop lag remains within existing healthy thresholds during
  simultaneous capture/playback tests.
- Every response and every committed interaction input reaches a durable terminal or
  checkpointed state.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| OEM AEC reports enabled but performs poorly | Runtime route-health scoring, physical Pixel/Samsung matrix, immediate half-duplex kill switch |
| Native engine and far-field recorder fight for the platform session | Single module ownership, serialized transition state machine, capture epochs |
| Low-latency fan-out harms durable ingestion | Branch before buffering, bounded non-authoritative queue, separate lag/health signals |
| Streaming STT arrives after endpoint | Watermark and exact-range batch fallback; never dispatch known partial text |
| Old socket plays a new reply | Capture- and voice-session binding on server and phone |
| Barge-in cancels an irreversible action | Durable effect fence; finish/reconcile action while suppressing stale output |
| Echo filtering drops a legitimate repeated phrase | Similarity is diagnostic only; VAD/route health and committed turn logic decide |
| Route transition repeats a confirmation | Cancellation is terminal; any replay is a new deliberate response |
| Background declarations are mistaken for support | Foreground launch gate and platform-specific physical acceptance |
| Full WAV response increases time-to-first-audio | Measure separately; optimize TTS later without combining chunked output with the initial safety release |

## Upstream concept provenance

The locally cloned Hugging Face `speech-to-speech` project is useful as design input,
not as a runtime dependency. The reviewed checkout is commit
`5a0c79f538488dd508e5353ba58f37a101aaf9cb` and is Apache-2.0 licensed.

Adapt these concepts:

- turn revision and reopen tracking;
- cancellation generations;
- response keys and ordered output sequence; and
- stale-result checks at asynchronous boundaries.

Do not copy its in-memory/GIL-dependent cancellation assumptions into Chronicle's
multi-process workers. Chronicle needs Redis-atomic generation changes and durable
effect checkpoints. If source code is copied rather than independently implemented,
retain the upstream license, attribution, and modified-file notices required by the
license.

## Constraints

- Preserve Wyoming JSONL-plus-binary framing and the current authenticated WebSocket.
  New versioned control events are allowed.
- Preserve the durable raw-audio WAL, immutable capture timestamps, and capture-owned
  chunks. The ephemeral frame bus and required capture profile/epoch metadata are
  scoped additions, not a persistence redesign.
- Do not change semantic Conversation, Timeline episode, memory, or reconciliation
  behavior.
- Do not add database migration scripts; this repository is in active development.
- Do not add legacy field aliases, Android route compatibility branches, or protocol
  fallback layers unless separately approved.
- AEC runs on the phone. Do not send an echo-reference stream to the backend.
- Preserve unrelated work in the dirty worktree. Each implementation phase must remain
  reviewable and independently revertible.

## Definition of done

The feature is done when all phase gates and acceptance criteria pass; the supported
route matrix is documented; response and background health are visible in System
Events/metrics; kill switches have been exercised; and one authorized live Swiggy flow
reaches UPI QR generation without payment, duplicate routing, stale playback, lost
opening speech, or replay of a side effect.

At implementation completion, update `CONTEXT.md`, `docs/interaction-modes.md`, mobile
audio documentation, and the new ADR to match the shipped identifiers and state
machines. The plan is not the lasting source of truth once those contracts ship.
