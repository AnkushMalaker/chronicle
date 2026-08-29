# Chronicle audio contract v2

`proto/` is the single source of truth for Chronicle audio messages that cross a
process or language boundary. Control messages use the generated Protobuf JSON
codec. Media and Redis stream events use Protobuf binary encoding.

The contract deliberately defines no generic maps, `Any`, `Struct`, or untyped
extension payloads. Add a field or a new `oneof` member when the protocol grows.

Live uplink is raw Opus at 16 kHz mono in 20 ms packets. Live downlink is raw Opus
at 24 kHz mono in 20 ms packets. PCM S16LE at 16 kHz mono is an internal inference
format and is never a live network transport.

The generated sources are checked in. The TypeScript contract is an independent
local package; client validation scripts run its pinned `npm ci` first because npm
links local directory dependencies and resolves generated imports from this folder.
Regenerate both languages with `scripts/generate-audio-contracts.sh`.
