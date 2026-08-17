# Audio WebSocket protocol

This is the implementer reference for every Chronicle audio client. Ordinary
capture uses a small Wyoming-style JSON+binary transport. Any client that wants
interactive spoken responses uses voice protocol v1; the capability report decides
whether that session is full duplex, isolated duplex, or native half duplex.

## Upgrade matrix

| Client | Ordinary capture | Interactive spoken responses | Required update |
| --- | --- | --- | --- |
| iOS Chronicle app | Base transport | Protocol v1 through `chronicle-duplex-audio` | Current native/TestFlight build |
| Android Chronicle app (API 31+) | Base transport | Protocol v1 through `chronicle-duplex-audio` | Current native build |
| OMI/Neo through Chronicle tray | Base transport | Protocol v1 on the verified tray-host route | Update/reinstall tray pendant client; no capture-only firmware change |
| Elato through Chronicle tray | Base transport | Protocol v1, half duplex, output on Elato | Update tray pendant client **and** Elato speaker firmware |
| HAVPE | Base device-to-relay transport | Protocol v1, half duplex, terminated by the relay | Update relay and flash the included ESPHome firmware |
| Script, integration, or capture-only device | Base transport | None | No update unless it wants interactive output |

There is one interactive WebSocket protocol. A bridge reports only what its real
route provides: verified host headphones may report `duplex_isolated`; host speakers,
Elato, and HAVPE report `duplex_half`. No bridge emulates phone AEC. The bridge is the
authenticated WebSocket client; BLE or ESPHome remains its device-side media
transport.

Omitting `voice_duplex_protocol` means ordinary capture. The backend records that
session as `ambient`, capture epoch `0`, with effects `unreported`. Capture and
unrelated authenticated APIs continue, but the connection cannot receive interactive
responses.

## Base capture transport

Connect to:

```text
WS /ws?codec=pcm|opus&token=<jwt-or-api-key>&device_name=<stable-device-name>
```

The authenticated socket supplies the user identity. A client must never send or
select `user_id`. The backend derives `client_id` from the authenticated user and
`device_name`.

Control headers are JSON objects. Wyoming TCP senders terminate each header with a
newline; WebSocket senders may send the JSON header as one text frame. When
`payload_length` is positive, exactly one binary frame of that length follows.

An ordinary capture needs only:

```json
{"type":"audio-start","data":{"rate":16000,"width":2,"channels":1,"mode":"streaming"},"payload_length":null}
```

```json
{"type":"audio-chunk","data":{"rate":16000,"width":2,"channels":1},"payload_length":3200}
```

The second header is followed by exactly 3,200 bytes. Send any number of chunks,
then finish with:

```json
{"type":"audio-stop","data":{},"payload_length":null}
```

This remains the supported interface for simple JSON+binary clients. Protocol v1 is
not a prerequisite for capture, transcription, memory processing, or other APIs.

## Interactive extension: voice protocol v1

An interactive client advertises the extension and complete capture provenance in
`audio-start`. The initial request has no server-issued voice-session ID:

```json
{
  "type": "audio-start",
  "data": {
    "rate": 16000,
    "width": 2,
    "channels": 1,
    "mode": "streaming",
    "voice_duplex_protocol": 1,
    "capture_epoch": 1,
    "processing_profile": "half_duplex",
    "effects": {
      "aec": {"requested": false, "available": false, "enabled": false},
      "noise_suppression": {"requested": false, "available": false, "enabled": false}
    },
    "voice_session_id": null
  },
  "payload_length": null
}
```

Phones use `duplex_aec`, `duplex_isolated`, or `half_duplex` based on the native
engine and route. The tray uses `duplex_isolated` only for a conservatively verified
headphone route; all other wearable output uses `half_duplex`. Every engine/profile
transition starts a new capture session and advances `capture_epoch`.

Startup order:

1. Client sends protocol-v1 `audio-start`.
2. Backend authenticates the socket, creates the voice binding, and persists the
   capture with that non-null binding.
3. Backend sends `audio-session.started`.
4. Backend sends `voice-session.start` with a single-use resume token.
5. Client reports actual capabilities with `voice-session.ready`.
6. Audio chunks continue over the unchanged base binary path.

A v1 client must fail interactive activation closed if this handshake does not
complete. Chronicle clients surface that as `server_upgrade_required`; they do not
fall back to the removed `play-audio` path.

Every interactive event has `protocol: 1`, a unique `event_id`, authenticated
`client_id`, and timezone-aware `sent_at`. Bound events additionally carry
`audio_session_id`, `voice_session_id`, and `capture_epoch`. Unknown fields and
stale/cross-socket bindings are rejected.

| Event | Direction | Meaning |
| --- | --- | --- |
| `audio-session.started` | backend to client | Confirms persisted capture, profile, epoch, and voice binding. |
| `voice-session.start` | backend to client | Starts the interaction and supplies the one-use resume token. |
| `voice-session.ready` | client to backend | Reports actual mode, routes, sample rate, and effect state. |
| `voice-session.capabilities-changed` | client to backend | Reports a completed route/engine transition on a new epoch. |
| `voice-session.resume` | client to backend | Presents one-use proof after reconnect. |
| `voice-session.stop` | backend to client | Cancels interactive work and requests capture restoration. |
| `voice-session.stopped` | client to backend | Reports restoration success or failure. |
| `response.audio` | backend to client | Announces one bound WAV; the binary WAV frame follows immediately. |
| `response.cancel` | backend to client | Flushes a response and advances the generation fence. |
| `response.playback` | client to backend | Reports physical `started`, `done`, `cancelled`, or `failed`. |

The response coordinator may mark a response cancelled before hardware confirms it
stopped. The later `response.playback: cancelled` ACK uses the response's original
generation and is accepted idempotently; `response.cancel.generation` is the newer
generation used to reject stale cancellation commands.

Exact strict fixtures live in
[`contracts/voice_protocol/v1/golden`](../../contracts/voice_protocol/v1/golden/).
Backend Pydantic, app TypeScript, and Python client implementations share those
shapes. `voice-session.ready-half-duplex.json` is the canonical bridge capability
example.

## Wearable device-side transports

The protocol-v1 binding ends at the tray or relay. Device transports must still
provide enough evidence for honest playback ACKs:

- Elato: the tray converts the bound WAV to paced 24 kHz mono Opus, sends response-
  bound BLE commands, and waits for firmware `started/done/cancelled/failed`
  notifications. Old Elato firmware has no status characteristic and is therefore
  capture-only. See [Elato speaker protocol v1](../firmware/elato-speaker-protocol-v1.md).
- HAVPE: the relay stages the WAV for the ESPHome media player. Included firmware
  stops microphone capture while announcing and publishes physical `started/stopped`
  state. The relay maps that state to protocol-v1 ACKs.
- Speakerless OMI/Neo: the updated tray verifies the actual macOS default output.
  Headphones keep OMI capture live with `duplex_isolated`; speakers suppress the
  uplink while `afplay` is active and report `duplex_half`. The tray shows the route
  plus `ready`, `TTS playing`, or `TTS interrupted`, and immediately starts a new
  capture epoch after an output-route change. The Pendant → Voice output menu offers
  Automatic, Require headphones, and Always speaker-safe policies. The pendant
  firmware remains capture-only.

The legacy backend-inferred `play-audio`, client-ID-suffix Elato routing,
`speak-start`, and immediate-success adapter ACK are not valid interactive v1
behavior and must not be reintroduced as a fallback.

## Upgrade boundaries

- Old/capture-only clients keep base capture.
- Interactive activation without v1 receives `client_upgrade_required` while other
  authenticated functionality continues.
- A v1 client connected to an old backend reports `server_upgrade_required` and does
  not attempt legacy interactive playback.
- Updating tray code updates the OMI/Neo/Elato WebSocket endpoint because the tray's
  Pendant section depends on `chronicle-wearable` and shared `chronicle-client`.
- Elato device playback additionally requires its external firmware update; the
  Chronicle repository contains the normative BLE contract, not that firmware tree.
- HAVPE requires the relay and included firmware from the same approved revision.

There is no runtime switch from a v1 session back to the old interactive path.
