# One-Shot Full-Duplex Voice Cutover

**Date:** 2026-08-16

**Branch:** `feature/full-duplex-voice-cutover`

**Target:** `dev`
**Status:** Replacement implemented; the single cutover gate remains open until every
automated, native, physical-device, and acoustic result is recorded as passing.

## Decision

Chronicle replaces the phone voice path for iOS and Android API 31+ in one release.
The backend, wakeword worker, response runtime, iOS app, and Android app are released
from the same approved revision. Intermediate commits are never deployed.

The replacement has no production feature switch, percentage control, phone
allow-list, dual phone runtime, or path back to the removed implementation. Older
phone builds may continue ordinary authenticated capture, but interactive activation
fails with `client_upgrade_required`. A protocol-v1 phone connected to an older
backend reports `server_upgrade_required` and does not activate interactive voice.

Wearables and other devices remain active transports behind the response coordinator.
Their adapters do not weaken the authenticated phone session binding.

## Scope

The cutover delivers these connected behaviors:

1. A versioned protocol binds interactive output to an authenticated socket, audio
   session, voice session, capture epoch, user, and client.
2. A low-latency frame stream feeds the active turn segmenter before the existing
   250 ms Redis write-ahead-log buffer.
3. Audio-interval claims and transcript watermarks produce one committed route for
   each spoken episode.
4. A Redis response coordinator fences all LLM, tool, TTS, downlink, and playback
   work with a monotonically increasing client generation.
5. One native phone engine owns capture and playback while interactive.
6. Required capture provenance records every engine/profile transition without
   rewriting audio chunks.
7. Swiggy consumes committed structured turns, supports bounded multi-item collection,
   and preserves checkout at-most-once behavior.

This work does not replace Wyoming framing, the durable audio WAL, MongoDB audio
evidence, Conversation claims, timeline claims, or the Markdown vault.

## Non-negotiable invariants

- `AudioChunkDocument.captured_at`, chunk IDs, audio bytes, Conversation claims, and
  vault contents do not change during provenance conversion.
- `client_id` alone is never an interactive playback target.
- A reconnect creates a new audio session and voice session. Resume requires a
  single-use hashed token with a 15-second lifetime.
- A fresh recording without valid resume proof ends the earlier interaction.
- No audio chunk crosses a capture epoch or processing-profile boundary.
- Only one atomic audio-interval claim may dispatch an episode.
- Known partial text is never dispatched. Missing transcript intervals are filled by
  exact-range batch transcription before dispatch.
- Only committed turns reach interaction modes.
- At most one response is playing for a client, and stale generations never speak.
- Before an irreversible-effect fence, work is cooperatively cancelled. After it,
  the mutation finishes and reconciles while stale speech remains suppressed.
- Checkout has an unknown-outcome state and is never automatically replayed.
- Full duplex is reported only for verified speakerphone AEC or acoustically isolated
  headphones. Every other route reports explicit native half duplex.

## Workstream A: protocol and session contracts

`audio-start` advertises `voice_duplex_protocol: 1` and required capture provenance.
Pydantic and TypeScript contracts are checked against shared golden fixtures.

| Event | Direction | Purpose |
| --- | --- | --- |
| `audio-session.started` | backend to phone | Confirms the capture session and epoch before voice activation. |
| `voice-session.start` | backend to phone | Supplies the bound voice session and one-use resume token. |
| `voice-session.ready` | phone to backend | Reports actual native route and effect capabilities. |
| `voice-session.capabilities-changed` | phone to backend | Reports a completed native engine/route transition. |
| `voice-session.resume` | phone to backend | Presents the single-use proof for a new socket and sessions. |
| `voice-session.stop` | backend to phone | Cancels interactive native work. |
| `voice-session.stopped` | phone to backend | Acknowledges native shutdown and far-field restoration. |
| `response.audio` | backend to phone | Schedules one generation-bound binary response. |
| `response.cancel` | backend to phone | Flushes the matching or superseded response. |
| `response.playback` | phone to backend | Acknowledges `started`, `done`, `cancelled`, or `failed`. |

Every interactive event carries the applicable client, audio-session, voice-session,
epoch, and generation identity. Malformed, unsupported, cross-socket, cross-epoch,
duplicate, or stale events fail explicitly.

## Workstream B: active turns and committed routing

Native PCM frames are 20-100 ms, timestamped in the capture clock, and published to a
bounded Redis stream before the 250 ms durable producer accumulator. The frame stream
is low-latency working data; the existing WAL remains capture evidence.

The wakeword service runs `ActiveTurnConsumer` separately from acoustic wake detection.
It uses Silero VAD and Smart Turn to open, reopen, soft-end, and commit turns. Consumer
pending entries are recovered after worker restart and acknowledged only after
successful processing.

The committed-turn router:

- assembles final transcript fragments by exact audio interval and STT watermark;
- invokes exact-range batch transcription for missing intervals;
- acquires one audio-episode claim shared by wake, streaming, and active interaction
  paths;
- dispatches either the active interaction mode or the ordinary command executor;
- recovers pending Redis deliveries after a worker restart; and
- acknowledges only after routing completes.

Protocol-v1 capture does not dispatch streaming fragments or acoustic wake detections
directly. Ambient and wearable wake transports remain active through their adapters.

## Workstream C: response coordination and effects

The Redis response coordinator is the only path for spoken replies and tones. A new
user turn, replacement, cancellation, route change, disconnect, or session stop
increments the client generation and cancels any current response.

Generation is checked before and after asynchronous LLM, read-only tool, TTS, response
publication, socket delivery, and playback operations. A response is deliverable only
when its complete authenticated binding still matches the ready voice session.

Interaction plugins checkpoint immediately before a non-idempotent external effect.
Redelivery before that checkpoint can retry safely. Once the checkpoint exists, a
mutation completes once and stores its reconciled result even if a newer turn has
superseded its speech.

The response coordinator retains an explicit device adapter for wearable and other
non-phone transports. The adapter is the only remaining reason for legacy
`play-audio` wire frames; phone output always uses `response.audio` plus binary data.

## Workstream D: native phone engine

`chronicle-duplex-audio` replaces `chronicle-mic-control`, JavaScript playback, and
JavaScript capture gating. Its public surface is intentionally limited to:

- start a voice session and return actual capabilities;
- emit timestamped PCM frames;
- schedule one response;
- cancel and flush a response;
- emit playback-state and route-change events; and
- stop the voice session and restore far-field capture.

Native code owns both capture and playback while interactive. JavaScript cannot start
another player or suppress microphone buffers on a timer.

### iOS

iOS uses one voice-processing `AVAudioEngine` graph with one player node. It converts
capture to 16 kHz mono PCM16 and reports route, interruption, media reset, epoch, and
playback acknowledgements. A route change, interruption, reset, or socket loss cancels
playback immediately. Final stop restores the previous audio category, mode, and
options; restoration failure is reported.

### Android API 31+

Android uses `MODE_IN_COMMUNICATION`, communication-device routing,
`VOICE_COMMUNICATION` `AudioRecord`, platform AEC/noise suppression, and one
`AudioTrack`. Capture and playback use separate executors. A route/focus change,
recorder/player failure, or socket loss cancels playback and forces a new epoch. Final
stop restores the prior mode, focus, and communication device; restoration failure is
reported.

Speakerphone is `duplex_full` only with enabled AEC. Wired, USB, or Bluetooth
communication headphones may be `duplex_isolated`. Unsupported or unverifiable routes
are `duplex_half`, where native code suppresses capture during its own playback.

## Workstream E: capture provenance

Every capture session requires:

- `capture_epoch`;
- `processing_profile`: `ambient`, `imported`, `source_native`, `duplex_aec`,
  `duplex_isolated`, or `half_duplex`;
- requested, available, and enabled effect status; and
- `voice_session_id`, required only for interactive profiles.

An engine or profile transition ends the current capture session and starts another.
Runtime reads are strict and contain no missing-field fallback.

The only accommodation for older stored sessions is the offline command
`advanced_omi_backend.scripts.backfill_capture_provenance`. Dry-run is the default:

```bash
cd backends/advanced
uv run python src/scripts/backfill_capture_provenance.py \
  --maintenance-start 2026-08-16T12:00:00Z
```

Apply requires stopped writers, an explicit confirmation, and the real vault root:

```bash
uv run python src/scripts/backfill_capture_provenance.py \
  --apply \
  --confirm-writers-stopped \
  --maintenance-start 2026-08-16T12:00:00Z \
  --vault-root data/conversation_docs \
  --report data/backups/full-duplex-provenance-report.json
```

The command maps historical streaming, upload/import, and source-native sessions to
the specified epoch-zero profiles. It refuses ambiguous origins, conflicting partial
provenance, or pre-maintenance active sessions. Apply hashes protected collections,
immutable capture fields, and the vault before and after updating only provenance.

## Workstream F: Swiggy acceptance behavior

- Ordinals select a candidate only when the complete selection turn is an ordinal, or
  it explicitly says `option N` or `number N`.
- Phrases such as `the two different modes` do not select option two.
- `collect_items` accepts one to five `{query, quantity, notes}` values.
- Read-only searches run concurrently with a limit of three.
- Cart and checkout mutations are serialized.
- Only the exact committed command `confirm order`, after a fresh review, crosses the
  checkout fence.
- Search interruption discards uncommitted candidates. Cart or checkout interruption
  lets a fenced mutation finish once, reconciles state, and suppresses stale speech.
- The fake-MCP acceptance flow ends at UPI QR generation. It never performs payment.

## The single pre-cutover gate

The branch is eligible to merge only when one committed validation report identifies
the exact revision and records every result below. A missing result is a failed gate,
not an implied pass.

### Automated and code gates

- Existing backend unit and integration suites.
- `cd tests && make test PROFILE=mock`, including the duplex phone and fake-Swiggy
  scenarios in the default suite.
- Direct entry-point coverage for the WebSocket handler, active/committed turn
  consumers, interaction worker, response subscriber, checkpoint/ACK recovery, and
  health reporting.
- A simulated protocol-v1 phone covering binary response playback, playback ACKs,
  barge-in, stale sockets, route changes, reconnect/resume, duplicate events, and
  worker restarts.
- Property checks for one route per episode, one playing response, monotonic
  generations, no stale playback, and no replay beyond an effect fence.
- Fake-Swiggy checks for slow multi-item dictation, explicit selection, ambiguous
  number phrases, search/cart/checkout interruption, Redis redelivery, and the full
  fake flow through UPI QR generation without payment.
- App TypeScript checks and JavaScript controller tests.
- XCTest for engine state, route/interruption/reset behavior, resampling, epoch
  rejection, restoration, and native cancellation acknowledgement.
- Android unit/instrumentation tests for focus/mode/device restoration, AEC/noise
  suppression, recorder/player failure, route change, epochs, and cancellation ACK.
- Import-placement, Black/isort, TypeScript formatting, and lint checks.

### Physical-device and acoustic gates

Required hardware is iPhone speakerphone, AirPods/HFP, wired iPhone headphones, Pixel
API 31+ speakerphone/Bluetooth/wired, Samsung API 31+ speakerphone/Bluetooth/wired, and
an Android unavailable-AEC fallback.

For each device and route:

- 20 no-user TTS trials: zero false committed turns, Conversations, or interruptions;
- 20 deliberate interruptions: at least 19 retain the opening word and stop playback
  within 700 ms; and
- route changes: zero overlapping or stale playback.

Automated acoustic replay must record:

- at least 500 no-user trials with zero committed echo turns;
- at least 100 interruptions per supported route class;
- 700 ms or better p95 stop latency; and
- at least 95% opening-word retention.

If any required device is unavailable, the branch may be clean and automated-green,
but its validation status must remain **NOT CUTOVER READY**.

## Cutover procedure

1. Confirm the validation report names the branch head and says every gate passed.
2. Merge that exact head into `dev`. Do not deploy an intermediate revision.
3. Announce the short interactive-voice maintenance window.
4. Stop capture writers, backend workers, interaction workers, and wakeword workers.
5. Export a full Chronicle archive, verify its checksums, and prove restoration in an
   isolated target using `chronicle-data.sh` as documented in
   [Data archive and memory rebuild](backend/data-archive.md).
6. Run the provenance command without `--apply`; review all counts and require zero
   errors and zero old active sessions.
7. Run apply once. Require `postconditions_verified: true`, valid provenance on every
   capture session, equal protected digests, and no pre-maintenance active session.
8. If conversion or verification fails, keep services stopped and use the verified
   backup-and-reset recovery path. Never start the new runtime on partially converted
   data.
9. Deploy backend, workers, wakeword service, and both native builds from the same
   approved revision.
10. Start services, then install or release the iOS and Android builds.
11. Smoke-test ordinary capture, activation, interruption, fake-Swiggy cart review,
    and explicit checkout confirmation through pending UPI QR generation.

If post-start interactive voice fails, new activation fails closed with
`temporarily_unavailable` while the defect is fixed forward. Ordinary authenticated
Chronicle functionality remains available. The removed phone voice implementation is
not restored.

## Evidence and readiness

The committed validation report must include commands, revision hashes, device and OS
models, route, trial counts, latency/retention statistics, failures, fixes, and final
reruns. A clean branch or passing unit suite alone is insufficient. Chronicle is
cutover-ready only after the complete single gate passes at one revision.
