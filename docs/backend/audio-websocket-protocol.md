# Chronicle audio WebSocket protocol

Chronicle has one backend audio transport: contract version 2 at `/ws/audio`, with
the WebSocket subprotocol `chronicle.audio.v2`. The source of truth is
`contracts/audio/v2/proto/backend/audio_contract/v2/audio.proto`;
Python and TypeScript consumers use generated bindings.

## Connection and control

The client opens `/ws/audio`, negotiates the subprotocol, and sends `ClientHello` as
the first generated `ClientControl` JSON message. Authentication is the bearer token
inside that message. Subsequent text frames contain exactly one generated control:
start/stop capture, voice-ready, playback acknowledgement, button, or heartbeat.
Unknown fields and unspecified required enums are rejected.

The server returns generated `ServerControl` JSON. `CaptureStarted` supplies the
complete `CaptureBinding`; clients must copy that binding into every later control
and media packet. A reconnect creates a new physical `ConnectionId`, capture session,
and binding. A stale connection cannot clean up its replacement.

## Media

Each binary WebSocket frame is one serialized `MediaEnvelope`; header and payload are
never split across frames.

- Uplink: 16 kHz mono, 20 ms raw Opus `CaptureMediaPacket`.
- Downlink: 24 kHz mono, 20 ms raw Opus `PlaybackMediaPacket`.
- PCM S16LE exists only behind the backend transport boundary.

Every capture packet carries its binding, monotonically increasing sequence, absolute
UTC capture clock, monotonic offset, delivery class, and Opus payload. The server
returns `CapturePacketAccepted` after the frame has entered the appropriate typed
Redis durability lane. A mobile spool retires its local packet only after this ACK.

`DELIVERY_CLASS_RECOVERED` packets enter durable persistence only. They can never
enter live ASR, wake detection, committed-turn routing, or actions.

## Interactive playback

Interactive capture reports generated capabilities with `VoiceReady`. The backend
sends `PlaybackOffer`, ordered media packets, and optional `CancelPlayback`. Clients
report physical `started`, `done`, `cancelled`, or `failed` states with
`PlaybackAcknowledgement`; writing bytes to a socket is not treated as playback.

The response generation and complete capture/voice/socket binding fence stale TTS,
late acknowledgements, and replacement turns.

## Cross-process boundaries

Redis capture streams and device downlink pubsub carry serialized generated messages.
They do not carry generic maps, protobuf `Any`/`Struct`, or stringified JSON. MongoDB
continues to store canonical Opus evidence with immutable absolute `captured_at`.

See [audio-interface-map.md](audio-interface-map.md) for the live migration ledger,
verification evidence, and remaining physical gates.
