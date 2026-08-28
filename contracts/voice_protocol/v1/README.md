# Voice duplex protocol v1 contracts

The normative transport and compatibility reference is
[`docs/backend/audio-websocket-protocol.md`](../../../docs/backend/audio-websocket-protocol.md).

- `golden/` contains events that must parse identically in Pydantic and TypeScript.
- `invalid/` contains events both implementations must reject.
- `typescript/` contains the single strict parser, transport-neutral
  `VoiceSessionController`, and `InteractivePcmFrameEncoder` shared by the phone and
  web recorder; platform audio remains in thin adapters.
- `golden/interactive-pcm-frame.json` is the cross-language captured-time packet
  contract. Interactive producers must use the shared encoder rather than construct
  audio-chunk headers themselves.
- `user_id` is intentionally absent from every wire event; authentication supplies it.
- This extension is required for every interactive voice client: phones and updated
  wearable bridges. Ordinary capture does not require it.
- `voice-session.ready-half-duplex.json` is the canonical capability event for Elato,
  HAVPE, speaker-routed OMI, and other bridges without verified isolation or AEC.
