# Elato speaker protocol v1

Elato firmware does not connect directly to Chronicle's WebSocket. The updated
Chronicle tray owns authentication and voice protocol v1, converts each bound WAV to
24 kHz mono Opus, and transports it over BLE. Firmware must implement this contract
before the tray advertises Elato device playback.

The Elato firmware source is maintained outside this repository. This document is
the normative handoff for that firmware change; do not copy an external firmware
checkout or generated build artifacts into Chronicle.

## GATT characteristics

| UUID | Properties | Purpose |
| --- | --- | --- |
| `19B10004-E8F2-537E-4F6C-D104768A1214` | write, write-without-response | Bound controls and Opus packet fragments |
| `19B10005-E8F2-537E-4F6C-D104768A1214` | notify | Physical playback status |

Absence of the status characteristic means firmware is not protocol-v1 capable. The
tray must not infer support from device name or from the write characteristic alone.

## Control writes

Multi-byte integers are unsigned little-endian. UUIDs are the 16 raw bytes of the
canonical response UUID.

| Opcode | Payload after opcode | Meaning |
| --- | --- | --- |
| `0x01` START | response UUID (16) + response generation (8) | Atomically flush old audio, bind, enter half duplex, and arm playback. |
| `0x02` END | response UUID (16) + response generation (8) | Finish input; drain buffered audio, then report DONE. |
| `0x03` STOP | response UUID (16) + cancellation generation (8) | Stop immediately when cancellation generation is at least the bound response generation. |
| `0x10` AUDIO | flags (1) + Opus fragment | Append one packet fragment; flags bit 0 marks the final fragment. |

START, END, and STOP use acknowledged GATT writes. AUDIO uses write-without-response.
Only one response may be bound at a time. A new valid START flushes the previous
decoder/ring before accepting bytes.

## Status notifications

Every notification is exactly 25 bytes:

```text
status opcode (1) | response UUID (16) | original response generation (8)
```

| Status | Meaning |
| --- | --- |
| `0x81` STARTED | DAC playback for the bound response has actually started. |
| `0x82` DONE | The final decoded sample has played. |
| `0x83` CANCELLED | Playback stopped and the ring/decoder were flushed. |
| `0x84` FAILED | The bound response could not be decoded or played. |

CANCELLED reports the original response generation from START, not the newer
cancellation generation. The tray uses the newer generation only to fence stale STOP
commands, then ACKs the original response binding to Chronicle.

## Native half-duplex requirements

- Stop or suppress microphone notifications before emitting STARTED.
- On STOP, halt the DAC, clear every buffered fragment/decoded sample, then emit
  CANCELLED within 700 ms.
- After DONE, CANCELLED, or FAILED, restore far-field microphone capture before the
  status notification or as the same atomic state transition.
- Reject AUDIO/END for a response other than the active binding.
- Reject STOP whose cancellation generation is older than the active response.
- Never emit DONE merely because END was received; wait for physical drain.

Firmware validation must cover START replacement, fragmented packets, malformed
bindings, stale STOP, decoder failure, disconnect, and microphone restoration. The
Chronicle cutover gate still requires physical Elato acoustic trials after that
external firmware is built and flashed.
