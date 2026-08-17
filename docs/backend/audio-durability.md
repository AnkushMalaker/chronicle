# Raw audio durability state machine

Chronicle has one streaming durability path:

```text
backend buffer -> Redis Stream WAL -> journaled Mongo AudioChunkDocument -> XACK
```

The owner of this path is a technical `AudioCaptureSession`, never a Conversation.
There is no alternate persistence fallback. An error stops the transition and leaves
the last durable state intact.

## Guarantee boundary

The guarantee begins when Redis returns success from `XADD`. Redis is configured with
AOF `appendfsync always`, so success follows a filesystem sync. Before that point bytes
exist only in transport/backend memory. If ingress cannot commit them, Chronicle closes
the connection instead of pretending the capture stayed contiguous.

## Session transitions

| From | Required commit | To | Failure behavior |
|---|---|---|---|
| Disconnected | audio-start begins initialization | Connecting | Reject connection |
| Connecting | unique Redis session/WAL, Mongo `AudioCaptureSession`, and persistence job are ready | Active | Do not accept audio |
| Active | transaction verifies `ACTIVE` and `XADD`s capture fields | Active | Preserve producer buffer; stop ingress |
| Active | residual buffer and terminal marker are appended, then status changes | Draining | Leave session retryable |
| Draining | producer declares no more legal `XADD`s | Producer-finished | Preserve WAL/session |
| Producer-finished | persistence group has zero lag, zero pending entries, and no local PCM | Persistence-complete | Retain stream if proof is absent |
| Persistence-complete | every required consumer group is drained | Stream deletable | Retain stream |

Every connection attempt has a unique capture/session ID. Reconnect cannot reset or
append behind an older WAL that is still draining.

## Message transitions

| State | Evidence | Only legal next transition |
|---|---|---|
| Accepted/unread | persistence-group lag is non-zero | `XREADGROUP` makes entry pending |
| Pending | pending entry retains source bytes | encode and commit Mongo chunk |
| Mongo committed | chunk has capture identity and exact stream-message provenance | update capture metadata |
| Metadata committed | all Mongo writes succeeded | `XACK` exact source IDs |
| Acknowledged | group no longer owns the IDs | eligible for drain proof |

The chunk key `(source_stream, source_first_message_id)` is unique. A crash after Mongo
commit but before ACK therefore replays as validation plus ACK, not duplicate audio.
The separate unique `(capture_session_id, sequence)` index protects ingest order.

## Capture identity

Each committed chunk contains:

- `user_id`;
- `capture_source_id`;
- `capture_session_id` and sequence;
- immutable absolute `captured_at`;
- duration/format/Opus bytes; and
- the exact Redis message IDs folded into it.

The Redis message carries an explicit capture time where available; otherwise its
Redis stream timestamp anchors `captured_at`. A missing/invalid absolute time is an
invariant failure, not permission to guess.

No WAL entry or Mongo chunk requires a Conversation ID. Semantic segmentation cannot
interrupt or rotate persistence.

## Worker runtime

The persistence worker tracks two independent axes:

- reader: `recovering_pending -> tailing_new`;
- session: `active -> draining`.

Completion is legal only at `(tailing_new, draining)` after the group is proven
drained. Any exception fails the RQ attempt; unread and pending Redis state remains,
and the retry re-enters pending recovery.

## Finite-capture durability

Uploads, ScreenPipe windows, and approved imports use `convert_audio_to_chunks` rather
than the Redis WAL. Their deterministic retry contract is:

1. hash the complete source PCM;
2. insert or load the specified `AudioCaptureSession`;
3. reject any identity/format/hash/time mismatch;
4. validate already stored contiguous chunks; and
5. resume only missing chunks.

A completed retry returns the same IDs. ScreenPipe persists its full mixed window
before VAD, so silence and semantic-processing failures cannot discard source audio.

## Forbidden cleanup

Raw `audio:stream:*` keys must never be capped, age-expired, independently trimmed,
administratively ACKed without the Mongo side effect, or deleted while any required
consumer has unknown/non-zero lag or pending entries. Age is not durability evidence.

Likewise, deleting or archiving a Conversation cannot delete capture documents. Raw
deletion requires a separate retention policy and a complete claimant check.

## Regression coverage

- `tests/test_audio_durability.py`: append failure, Mongo failure, pending recovery,
  commit-before-ACK replay, retention gates, and legal transitions.
- `tests/test_audio_persistence_lifecycle.py`: close/finalize race.
- `tests/test_streaming_persistence_invariant.py`: capture initialization and reconnect
  isolation.
- `tests/test_audio_persistence_mongodb.py`: finite-capture retry convergence.
- `tests/test_device_audio_ingest.py`: silent ScreenPipe capture persists without a
  semantic Conversation.
