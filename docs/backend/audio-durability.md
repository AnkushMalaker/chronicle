# Raw audio durability state machine

Chronicle has one raw-audio durability path:

`backend process buffer -> Redis Stream WAL -> journaled MongoDB audio chunk -> Redis XACK`

There is no alternate persistence fallback. An error stops the transition and leaves
the last durable state intact.

## Guarantee boundary

The guarantee begins when Redis returns success from `XADD`. Redis runs AOF with
`appendfsync always`, so that response follows a filesystem sync. Before `XADD`
returns, bytes exist only in the backend/device transport and cannot survive a backend
host crash; closing the connection on ingress error prevents Chronicle from pretending
that such a capture remained contiguous.

## Session transitions

| From | Event and required commit | To | Error behavior |
|---|---|---|---|
| Disconnected | audio-start begins initialization | Connecting | Connection is rejected; no audio accepted |
| Connecting | A unique session/WAL exists, a Mongo conversation owner is atomically assigned, and the single persistence job is live | Active | State does not become ingress-ready |
| Active | One transaction verifies session `ACTIVE`, snapshots its owner, and `XADD`s the owned audio | Active | Producer buffer and sequence stay unchanged; connection stops |
| Active/owner A | Conversation closes while capture continues; Mongo owner B is inserted, then Redis pointer A→B is compare-and-swapped | Active/owner B | Candidate B is deleted; owner A remains |
| Active | Residual producer buffer `XADD` succeeds, then terminal marker `XADD` succeeds, then session status is written | Draining | Status does not advance and the same buffer/session remains retryable |
| Draining | Producer commits `FINISHED` (no more `XADD`s are legal) | Producer-finished | A failed write leaves the connection/session available for the same finalization transition |
| Producer-finished | Persistence group has zero lag, zero pending entries, and no local PCM | Persistence-complete/retained | Stream remains if proof is unavailable |
| Persistence-complete | Every required and registered consumer group has zero lag and zero pending entries | Stream deleted | Stream remains |

`SessionStatus.ACTIVE` may exist briefly inside Connecting, but the controller does not
return to the receive loop until owner assignment and job liveness both succeed. Each
recording attempt has a new `session_id`; reconnect never resets an older hash, stream,
consumer, or producer buffer.

## Error semantics

`Error` is an attempt outcome, not a second storage path or a state that consumes data.

| Error location | Observable result | State retained |
|---|---|---|
| Connecting | audio-start/connection is rejected | No ingress-ready session |
| Producer before Redis accepts | socket is closed; no later packet is accepted | Exact producer buffer and chunk number |
| Disconnect/finalize | client is not removed, so reconnect must retry cleanup first | Same unique session and producer buffer |
| Mongo insert or metadata update | RQ attempt fails and retries | Redis entries remain pending |
| After Mongo insert, before ACK | RQ retry finds the provenance-keyed chunk | Mongo chunk plus pending Redis IDs |
| Retention inspection | deletion is refused | Full Redis stream |

There is no `continue`, guessed owner, age-based delete, alternate store, or
administrative ACK on these paths.

## Redis message transitions

| State | Redis evidence | Only legal next transition |
|---|---|---|
| Accepted/unread | persistence group `lag > 0`; entry contains `conversation_id` | `XREADGROUP` makes it pending |
| Pending | persistence group `pending > 0` | Encode and commit Mongo chunk |
| Mongo committed | chunk has the stream/message provenance key and the write is majority+journal acknowledged | Update visible metadata |
| Metadata committed | Mongo writes succeeded | `XACK` the exact source IDs |
| Acknowledged | group no longer owns the IDs | Eligible for stream-level drain proof |

A worker crash before `XACK` leaves entries pending. Every worker attempt starts by
reading its pending list before reading new entries. If the Mongo insert succeeded but
the worker died before `XACK`, the unique `(source_stream,
source_first_message_id)` key turns replay into an idempotent metadata retry rather
than duplicate audio.

## Worker runtime transitions

The persistence worker tracks two independent axes:

- Reader: `recovering_pending -> tailing_new`
- Session: `active -> draining`

Completion is legal only in `(tailing_new, draining)` after the persistence group is
proven drained. Any exception changes the RQ attempt to Error; unread/pending Redis
state is not changed, and the next RQ attempt starts again at `recovering_pending`.

Conversation handoff has no unassigned interval. The lifecycle inserts the successor
first and compare-and-swaps the Redis pointer, while producer `XADD` watches that same
pointer. The persistence worker groups by the immutable owner on each entry; a pointer
change or worker restart cannot merge two conversations.

## Forbidden cleanup

Raw `audio:stream:*` keys must never be:

- capped with `MAXLEN`;
- trimmed by an independent consumer;
- expired because a socket disconnected or a stream is old;
- administratively claimed and ACKed without committing the side effect;
- deleted while the session is active or any group has unknown/non-zero lag or pending.

Age is not durability evidence.

## Regression coverage

`tests/test_audio_durability.py` covers Redis append failure, inactive/ownerless
ingress, owner rotation, Mongo failure, pending recovery, commit-before-ACK crash
replay, retention lag, terminal residual flush, and legal runtime transitions.
`tests/test_audio_persistence_lifecycle.py` covers the conversation-close/finalize
race. `tests/test_streaming_persistence_invariant.py` covers unique reconnect sessions,
connection-level fail-closed behavior, failed-disconnect retention, ordered stop, and
persistence liveness.
