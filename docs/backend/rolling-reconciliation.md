# Rolling reconciliation

Status: accepted, in implementation (Pass A)

Chronicle should reconcile memory continuously after evidence becomes available rather
than waiting for a local day to finish. A recording closing is an important scheduling
signal, but it is not proof that a real-world episode ended there. Transcript revisions,
speaker recognition, VAD, ScreenPipe context, uploads, and recovery can all make an
absolute time range worth reconsidering.

The reconciler starts with a dirty evidence range, asks for more context on either side
when its semantic boundaries remain ambiguous, and publishes revisioned Timeline
episodes. Day processing becomes an incremental projection plus a completeness audit,
not a second end-of-day interpretation pass.

## Product position

Chronicle currently prefers an Obsidian layout containing `Conversations/`, `Episodes/`,
`Daily/`, `People/`, `Topics/`, and other semantic categories. That is a default vault
policy, not a universal memory architecture. Another user or agent may organize a vault
differently.

The architecture therefore owns semantic records and projection contracts. A vault
adapter decides how those records appear in a particular vault. The initial Chronicle
adapter may continue using the current folders, but reconciliation must not encode
`People/` or `Daily/` as fundamental database concepts.

Immediate Conversation source records remain searchable. In the initial implementation,
only sufficiently settled Episode revisions update consolidated semantic projections
such as People or Topics. Explicit provisional claim objects remain a later option if
source-note and provisional-Episode retrieval does not provide adequate freshness.

## Data flow

```text
Evidence arrives or changes
    → mark its absolute range dirty
    → debounce related evidence revisions
    → reconcile the dirty range with surrounding context
    → expand left/right agentically when boundaries are unsupported
    → atomically publish an Episode revision
    → update deterministic Episode and day projections when content changed
    → project sufficiently settled semantic facts through the active vault policy

Day closure
    → audit coverage and failures
    → requeue missing work
    → verify the current day projection
```

## What exists today

The day-scoped timeline pipeline is proto-reconciliation and most of its parts are
reused rather than rebuilt: `TimelineEpisode.episode_key` (durable lineage identity),
`TimelineAnalysisRun.evidence_revision` with a unique day+revision idempotency index,
the CAS claim/lease pattern in `services/timeline/discovery.py`, carry-forward of
pinned episodes, stale-run fencing on `TimelineDay.active_run_id`, and the
change-detecting deterministic Daily `## Episodes` index. Rolling reconciliation
changes three things: scope (dirty absolute range, not a whole local day), trigger
(evidence-driven with debounce and forced progress, not cron at day granularity), and
output (revisions of durable `episode_key`s, not regenerated days). During the
transition both pipelines publish into `timeline_episodes`, distinguished by a
`pipeline: "day" | "rolling"` field and selected per user by
`User.active_timeline_pipeline`.

## Invariants

- Audio chunks retain immutable capture time and identity.
- Content hashes identify duplicate payloads, not real-world event identity.
- Scheduling signals decide when to inspect evidence, never where semantic boundaries
  belong.
- Every relevant evidence revision eventually reaches reconciliation or an observable
  terminal failure.
- A stale reconciliation run cannot publish over a newer evidence revision.
- Episode lineage survives regeneration, revision, split, and merge.
- Immediate Conversation records use captured time rather than ingestion time.
- Conversation records preserve sources but do not independently own consolidated
  semantic facts.
- Raw transcripts remain in MongoDB and are fetched on demand.
- Projection writes are idempotent for one source revision.
- Vault layout is policy. Semantic identity and provenance do not depend on folder names.
- An Episode intersecting two local dates refreshes **both** day projections. A day
  projection selects episodes by UTC-range intersection with the day's bounds;
  `local_date` on an episode row is a derived projection hint, never authority.
- User-facing conversation events (`conversation.complete` and everything riding on
  it) fire only from a settled conversational Episode revision, exactly once per
  `(episode_key, event_type)`. Resettlement or supersession never re-fires them.

## Settlement is a policy decision

`settled` must not mean merely “the recording closed” or “the local day ended.” An
Episode can be:

```text
open → provisional → settled → superseded
```

Naming note for implementers: today's persisted `status="confirmed"` means
*human-pinned*, which is orthogonal to settlement. Pinning becomes a separate
`pinned` flag (with `confirmed_fields` retained as the pinned-field list); `settled`
is a policy outcome the system reaches on its own. During the transition the enum
carries both `confirmed` (day pipeline) and the new states (rolling pipeline);
`confirmed` is removed at cutover.

- **Open** means relevant evidence is actively accumulating or an edge explicitly needs
  future evidence.
- **Provisional** means Chronicle produced a useful bounded interpretation, but later
  context may still revise it.
- **Settled** means the active settlement policy considers both boundaries adequately
  supported and no known prerequisite artifact remains pending for the range.
- **Superseded** means a later reconciliation revision replaced, split, or merged it.

Settlement is reversible by new evidence. Human-pinned facts and boundaries require an
explicit revision path rather than silent replacement.

The first policy can use an evidence watermark and pending-artifact checks, but the
storage model must leave room for different policies. The agent judges semantic support;
Chronicle enforces budgets, fencing, and liveness.

## Incremental day projection

Chronicle creates the current day projection when the first material Episode revision
exists. It does not wait until midnight.

After every successful reconciliation, the projection layer computes the desired day
view from active Episode revisions. It writes the vault note only when the resulting
content differs. If reconciliation produces no material Episode change, there is no day
write.

For the default vault policy this projection is `Daily/<local-date>.md`. It is a
deterministic chronological index and navigation surface, not an independent semantic
agent pass. A different vault policy may render the same projection elsewhere or omit a
day note entirely.

Day closure only:

- Finds unreconciled or expired dirty ranges.
- Retries failed continuations.
- Checks cross-midnight Episodes.
- Detects stale semantic projections.
- Compacts old provisional revisions according to retention policy.
- Verifies that the current day projection matches active Episode revisions.

## Dirty-range scheduling

Persist one `DirtyEvidenceRange` model with:

```text
dirty_range_id
user_id
started_at
ended_at
evidence_revision
source_revisions
trigger_reasons
not_before
force_after
state: pending | leased | waiting | completed | failed
lease_owner
lease_expires_at
attempts
last_error
created_at
updated_at
```

All evidence producers call one idempotent entry point:

```python
mark_evidence_dirty(
    user_id,
    started_at,
    ended_at,
    source_revision,
    reason,
) -> DirtyEvidenceRange
```

Overlapping or nearby pending ranges may coalesce, but source revisions and trigger
reasons remain available for fencing and observability.

A **leased** range is never coalesced into. The lease snapshots the range's
`evidence_revision` (`leased_evidence_revision`); the run reconciles that snapshot and
its publish fences on it. A trigger arriving during the run creates or merges a fresh
`pending` range over the same interval, which re-reconciles afterward. This is what
prevents fence-livelock under continuous evidence: forced progress guarantees a run
*starts*, the snapshot guarantees it can *finish*, and the re-dirty guarantees nothing
is lost. An overlapping trigger also wakes a `waiting` range back to `pending`.

Initial triggers are:

- Recording closure.
- Transcript creation or revision.
- Speaker-label revision.
- VAD or acoustic-profile revision.
- ScreenPipe audio or observation revision.
- Upload or manual-memory completion.
- Failed or expired reconciliation recovery.
- A manual or API request (`POST /api/timeline/reconcile`), which is an ordinary
  `mark_evidence_dirty` caller; `force` only sets `not_before` to now.

Initial scheduling defaults are:

- Normal debounce target: five minutes after the latest relevant revision.
- Forced progress: fifteen minutes after the range first becomes dirty.
- Recovery scan: every five minutes.

Continuous media or VAD can keep an Episode open, but cannot postpone reconciliation
beyond `force_after`.

## Evidence broker

Expose one bounded range interface:

```python
load_reconciliation_evidence(
    user_id,
    started_at,
    ended_at,
    expected_revision,
) -> EvidenceBundle
```

The bundle includes bounded transcript timing, speaker evidence, VAD/acoustics,
microphone versus system/media attribution, ScreenPipe OCR and application context,
images, meeting/manual context, explicit capture gaps, existing Episode revisions, and
human-pinned boundaries.

This interface reuses the present Timeline evidence assembly. It must not introduce a
parallel evidence ontology.

## Agentic context expansion

A reconciliation run starts with the dirty range and five minutes of context on each
side. The agent returns one of:

```text
publish(revisions)
request_more_context(left_seconds, right_seconds, reason)
wait_for_future_evidence(reason)
```

Chronicle enforces:

- Expansion in five-minute increments per side per iteration.
- At most six expansion iterations, hence at most thirty minutes of expansion per
  side in one run.
- Evidence and token budgets.
- Pinned-boundary protection.
- Exact inspected-evidence accounting.
- Compare-and-swap fencing on the evidence revision.

Budget exhaustion or unavailable future evidence creates a retryable continuation; it
never licenses an invented boundary.

A run revises rather than rederives. The evidence bundle carries the existing Episode
revisions and pinned boundaries for the range, and the agent is framed as updating
that prior interpretation with what changed. Episodes untouched by the dirty delta
carry forward unchanged — same key, same revision — generalizing today's
confirmed-episode carry-forward. Replaying an unchanged `evidence_revision` completes
the range as a no-op.

## Classification-gated event dispatch

The speech gate decides when speech-bearing audio exists; it no longer decides that a
conversation happened. At recording close, Chronicle still runs every evidence
producer — transcription, speaker identification, VAD profiling, summary — and marks
the range dirty, and still writes the provisional Conversation source record for
retrieval freshness. What it no longer does is dispatch `conversation.complete` or
the per-conversation consolidated memory write.

The reconciler classifies each Episode. A fine-grained `kind` (meeting, chat, call,
media, dictation, noise, …) serves timeline display; dispatch keys on the single
predicate `conversational`. A meeting and a kitchen chat both dispatch — the meeting
signal only makes boundaries authoritative and settlement faster. A media or noise
classification dispatches nothing, and retypes the provisional source record rather
than leaving a false conversation note.

Dispatch happens on the first settled conversational revision of an `episode_key`,
latched idempotently per `(episode_key, event_type)`. The stated UX change: the
summary email arrives roughly ten to fifteen minutes after close instead of about
two, and describes one real event rather than one per recording fragment.

## Episode revisions and stable navigation

Episode identity separates durable lineage from one generated row:

```text
episode_key
episode_id
revision
status: open | provisional | settled | superseded
predecessor_keys
successor_keys
evidence_revision
```

Add stable backend and UI routes:

```text
GET /api/timeline/key/{episode_key}
/timeline/key/{episode_key}
```

The route resolves the active revision. A superseded key resolves to its successor or
presents a split/merge choice. Reconciliation publishes a generation atomically, and a
stale run fails its compare-and-swap rather than replacing newer work.

## Source records and vault projections

When a Recording closes, Chronicle writes its immediate source record using
authoritative captured time in the user’s timezone and makes it searchable. Under the
default policy this remains `Conversations/<conversation-id>.md`.

Each active Episode revision has a deterministic projection. Under the default policy:

```text
Episodes/<episode-key>.md
```

It contains status, revision, local and UTC bounds, summary, participants,
classification, grounded assertions, evidence references, and the stable Chronicle
link. It never contains the raw transcript.

The initial default semantic projection maps settled Episode facts into existing
People, Topics, and curated categories. Those folders are adapter choices. Core
reconciliation emits provenance-bearing semantic deltas without assuming their final
file paths.

Normal retrieval searches immediate source records, provisional and settled Episodes,
and settled semantic projections. Provisional Episode facts must remain visibly
provisional.

## Typed write orchestration

All record types use the same orchestration phases:

```text
prepare
→ execute policy adapter
→ atomic commit
→ deterministic verification
→ advisory semantic review
→ repair
→ audit and OTEL
→ source-revision latch
```

They do not share one prompt, record schema, forbidden-path policy, or transaction
mechanism. Conversation records, Episode projections, day projections, and semantic
projections each retain explicit ownership rules.

## Delivery sequence

1. Correct Conversation capture-time attribution and add source/evidence revision
   telemetry.
2. Add stable Episode lineage plus stable-key API and UI routing.
3. Add dirty-range persistence, coalescing, leasing, debounce, forced progress, and
   recovery.
4. Extract the bounded evidence broker from current Timeline assembly.
5. Implement reconciliation runs and bounded context expansion.
6. Publish revisioned Episodes with stale-run fencing.
7. Generate deterministic Episode and incremental day projections.
8. Wrap projection writes in shared typed orchestration.
9. Run rolling reconciliation in shadow mode while retaining current semantic writers.
10. Compare current and rolling outputs on representative historical days.
11. Cut conversation-level consolidated-fact mutation and end-of-day semantic
    ingestion.
12. Rebuild the vault from source records, active Episodes, incremental day projections,
    and settled semantic projections.
13. Remove transitional flags and obsolete paths after validation.

Chronicle is under active development. Do not add backward-compatibility adapters or
database migration scripts. Historical semantic state is rebuilt from preserved
evidence during cutover.

## Required tests

- Every trigger dirties the correct absolute range.
- Duplicate triggers converge without losing source revisions.
- Late evidence extends the debounce without defeating forced progress.
- Constant media/VAD reaches reconciliation.
- Expired leases recover.
- Context expansion fetches only the requested side and obeys every budget.
- Pinned boundaries cannot be crossed silently.
- Stale runs cannot publish over newer evidence.
- Episode revisions retain stable lineage through revision, split, and merge.
- Stable Episode routes survive reanalysis.
- Conversation records use captured local time.
- Conversation closure creates searchable source material without independently
  changing consolidated semantic facts.
- Replaying an unchanged Episode revision or projection produces no write.
- A material Episode revision incrementally updates the current day projection.
- A reconciliation with no material change leaves the day projection untouched.
- Concurrent source, Episode, day, and semantic projection writes do not contaminate
  one another’s diffs.
- Daily audit detects holes and regenerates the same deterministic projection.
- Raw transcripts never appear in generated Episode or day projections.
- ScreenPipe microphone and system/media evidence remain separately attributed.
- OTEL/Langfuse records triggers, evidence revisions, expansions, continuations,
  reviewer outcomes, latency, token usage, and terminal state.
- A trigger during a leased run creates a fresh pending range and re-reconciles;
  continuous triggers cannot livelock or starve a range.
- A cross-midnight Episode revision refreshes both affected day projections.
- A media-classified Episode dispatches no conversation events and retypes its
  provisional source record.
- A settled conversational Episode dispatches exactly once per event type across
  resettlement and supersession.
- Episodes unaffected by a dirty delta carry forward with unchanged key and revision.

## Cutover acceptance

- No relevant evidence interval remains unconsidered without a visible error.
- Replaying unchanged evidence creates no semantic or projection diff.
- Stable Episode links survive Timeline reanalysis.
- Fresh queries recover source information before Episode settlement.
- Consolidated facts retain a path through an Episode projection to bounded evidence.
- Incremental day projections remain current without an end-of-day semantic pass.
- Shadow evaluation shows no material recall regression and fewer duplicate or
  conflicting shared-note writes than the existing dual-writer architecture.

## Deferred decisions

- Whether explicit provisional claim objects are necessary after retrieval evaluation.
- The final settlement policy and how its thresholds adapt to source reliability.
- User-configurable vault organization and projection adapters beyond the default
  Chronicle layout.
- Long-term retention and compaction policy for superseded Episode revisions.
