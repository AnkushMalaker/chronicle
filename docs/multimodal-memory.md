# Multimodal Memory

## Status

This document records design learnings from the July 2026 audit of Chronicle's
ScreenPipe observation pipeline. The semantic episode ledger and evidence broker are
implemented for the Timeline, and the layer-4 → layer-5 step now exists: a settled day of
episodes is written to the vault by `services/timeline/memory.py`, and the per-observation
curation pass this document argues against has been retired. Configurable zoom remains
future work. See [Semantic timeline episodes](backend/timeline-episodes.md) for current
behavior.

> **Partly superseded.** The layering below still holds, but three assumptions in
> the "Deterministic local signal layer" section were tested against the real
> archive and did not survive: the collector cannot detect activity boundaries
> from cheap signals, evidence ranking needs typographic salience rather than text
> prominence heuristics, and frame-id evidence pointers rot when ScreenPipe's
> retention prunes frames. The revised plan and the measurements behind it live in
> `docs/plans/` and `docs/research/screen-memory/`, which are kept locally rather
> than in the repository.

The central conclusion is:

> A capture observation is not an event, and an event is not automatically a memory.

Chronicle needs separate representations for all three. Trying to make one observation
lifecycle serve as the event detector, activity ledger, and vault curator causes useful
outcomes to be discarded and unrelated activity to be merged together.

## Audit findings

The audit compared local ScreenPipe frames and OCR with the observations and vault
entries stored by the live Chronicle backend.

At the audit snapshot, Chronicle had received 236 ScreenPipe observations:

- 227 were discarded
- 5 were linked to Daily notes
- 3 were pending
- 1 was marked duplicate
- no screenshots had been promoted

Selective vault curation is desirable, so the discard rate alone is not a defect. The
problem is that discarded observations are also the only remote representation of many
recoverable events.

### Two matches became one observation

Two consecutive Age of Empires IV matches illustrate the failure:

1. A defeat against Ibar on Golden Pit, ending by surrender at about 17 minutes.
2. A victory against King Maximilian on Himeyama, lasting 25:51.

ScreenPipe captured both results locally. Chronicle received OCR for the first result,
but the two matches and later development work were merged into one anonymous
609-frame observation lasting about 47 minutes. The initial preview was an ordinary
mid-game frame. The curator classified the combined span as routine gameplay and
discarded it. The two-minute sample cooldown missed the second result completely.

The ideal extracted session was a compact 1–1 match record. It did not require uploading
the frame stream or retaining every gameplay counter.

### The same pattern affected non-game activity

Chronicle retained a note describing the risk that a custom ScreenPipe AppImage could
be lost during an update. Later evidence showing that the problem had been resolved with
a persistent installation, disabled application updates, dependency fixes, and a pushed
fork was mostly discarded as routine development.

This demonstrates a general problem: the current pipeline can preserve the opening
problem while losing the resolution. Similar patterns apply to purchases, bookings,
deployments, repairs, research, media progress, conversations, and any other activity
whose meaningful outcome appears late in a long or poorly identified visual context.

### Additional correctness findings

- Contextless Wayland/game frames can have empty app, window, and URL fields.
- Accessibility text can include browser chrome, background tabs, generated assistant
  output, and unrelated visible applications.
- Forty-one percent of audited samples were at or near the 2,000-character text limit,
  so a large amount of transmitted text was noisy or truncated.
- Once the backend has a preview, later decisive samples do not replace it.
- Candidate ranking rewards generic text length and structured sources, not evidentiary
  value for a newly discovered event.
- Some stale observations remained marked open after collector state resets.
- A likely concurrent read-modify-save race can lose an ingested sample while preserving
  the following fingerprint link.
- ScreenPipe thumbnail extraction can seek to the following video frame because a target
  timestamp is rounded to milliseconds. OCR and returned pixels can therefore disagree
  at an event boundary.
- Screen observation notes currently use UTC for Daily filenames and headings. In
  Asia/Kolkata this makes headings 5 hours 30 minutes early and can place after-midnight
  activity in the previous day's note.

These are evidence integrity and lifecycle problems, not merely prompt-quality problems.

## The infinite event vocabulary

Chronicle cannot maintain an exhaustive local list of meaningful events. A user may care
about:

- a game result
- a package delivery
- a purchase or refund
- a booking confirmation
- a deployment completing
- a bug being fixed
- a document being submitted
- a person making a commitment
- a medical result becoming available
- a show episode finishing
- a useful idea appearing in a conversation
- an unfamiliar event that no developer anticipated

Hard-coding all such events into the collector would create an unbounded configuration
problem. It would also put semantic policy, user preference, and model credentials on
every capture node.

The collector must therefore detect generic evidence changes, not enumerate the meaning
of every possible event.

Domain-specific deterministic extractors can still exist when they materially improve
reliability. For example, a game extractor may recognize a stable result screen. Such an
extractor is an optimization and validation layer, not the only way that Chronicle can
discover the event.

## Target architecture

Chronicle should use five distinct layers.

### 1. Local source archive

ScreenPipe owns the high-volume local record:

- frames and video
- OCR and accessibility text
- capture timestamps and triggers
- app, window, URL, and monitor metadata when available
- optional input and output audio

Chronicle must not mirror this complete store. Frame IDs and bounded time ranges are
source pointers.

### 2. Deterministic local signal layer

The credential-free collector derives compact, generic signals:

- context appeared, disappeared, or changed
- text changed materially
- visual state changed materially
- user interaction occurred
- activity became idle or resumed
- a visually or textually novel anchor appeared
- an earlier stable state returned
- a source became unavailable or restarted

Signals describe what changed and where evidence is available. They do not need to know
whether the change represents a victory, payment, deployment, diagnosis, or booking.

The collector may calculate deterministic features such as:

- normalized text and fingerprints
- OCR additions and removals
- visual hashes or embedding-free image deltas
- text layout/prominence metadata
- stable-region changes
- frame quality and blankness
- before/after frame pointers

No LLM credential or personal-memory policy belongs in this layer.

### 3. Backend event discovery and evidence broker

The backend receives compact signals and proposes open-ended event hypotheses. A proposal
is provisional and may use a free-form event type rather than a fixed enum.

Examples:

- `game_match_completed`
- `software_fix_completed`
- `purchase_decision`
- `booking_confirmed`
- `media_progress`
- `research_conclusion`

The proposal agent does not need enough evidence in the first packet. It can issue a
bounded request back to the collector, such as:

- OCR between two timestamps
- frames immediately before and after an anchor
- the nearest prior stable title or lobby-like state
- a different ranked screenshot
- a source still at a specific frame ID
- a short accessibility-text history
- overlapping input-audio or output-audio context

The collector fulfills the request deterministically from the local archive. The backend
then confirms, revises, splits, merges, or rejects the proposed event.

This back-and-forth is the general solution to sparse capture. The collector sends a
small change packet; the backend asks for the minimum additional evidence required to
understand it.

### 4. Structured event ledger

Confirmed events are stored independently of vault-note curation. This ledger answers
questions such as "what was my last game?", "did that deployment finish?", or "which
inverter quote did I reject?" even when the event does not deserve a permanent Markdown
note.

An event should support:

```text
event_id
event_type
started_at
ended_at
title
summary
entities
attributes
outcome
user_actions
source_assertions
evidence_refs
confidence
sensitivity
durability
status
related_event_ids
related_conversation_ids
created_at
revised_at
```

Important properties:

- `event_type` is open-ended, namespaced when useful, and not a collector enum.
- Confidence is attached to individual assertions when facts have different certainty.
- Every important assertion cites local or retained evidence.
- An event may remain provisional until later evidence closes it.
- Events can update other events. A resolution should close or revise an earlier problem
  rather than create an unrelated memory.
- Event identity and revisions are idempotent.

For the audited game session, Chronicle should have stored two match events and one
optional session rollup. For a software repair, it may store one evolving event from
problem discovery through verified resolution.

### 5. Curated memory and vault

Vault curation is a later, more selective decision:

- ignore the event for long-term memory
- append it to a Daily note
- update an existing person, project, topic, place, or media note
- create a dedicated durable note
- retain a representative image

Discarding a vault write must not delete or semantically erase a confirmed structured
event. Event extraction asks "what happened?" Vault curation asks "what is worth
remembering long-term?" These questions require different thresholds.

## Event discovery without exhaustive rules

The backend should combine several mechanisms rather than depend on one global prompt or
one registry of regexes.

### Generic proposal pass

A multimodal proposal agent periodically reviews compact new signals and recent open
events. It can propose event boundaries and request more evidence. This is where the
open-ended vocabulary lives.

### Stateful reconciliation

The backend should compare new evidence with open events:

- Does this resolve an earlier problem?
- Is it a state transition within an existing activity?
- Is it a second event of the same kind?
- Is it unrelated context accidentally grouped by the collector?
- Is it only routine progress with no state change?

This prevents a final success, cancellation, or decision from being detached from the
activity that motivated it.

### Optional extractor packs

Deterministic or model-assisted extractor packs can provide higher precision for
important domains. They may define:

- anchor detectors
- bounded evidence queries
- validation rules
- domain-specific structured fields

Candidate packs include games, purchases, travel, development/deployment, media
progress, and communication. Packs should emit the same generic event contract and
remain optional. Unknown domains still flow through generic proposal and retrieval.

### User preference and feedback

The user should be able to correct an event, mark it important or unimportant, and state
preferences such as "track all ranked matches" or "do not retain shopping research."
Those preferences belong in backend policy, not in the low-level collector.

## Evidence selection

Evidence ranking must be related to the proposed event, not fixed for the lifetime of a
long observation.

When a proposal changes, Chronicle may need a new preview. A result, confirmation, error,
or completed state should not be forced to use the screenshot selected when the
observation opened.

A bounded evidence bundle normally contains:

- one anchor or outcome frame
- one earlier context-establishing frame
- the relevant OCR/accessibility delta
- optional overlapping audio evidence with source direction
- local timestamps and source pointers

The backend may request a second image only when it resolves a specific uncertainty.
This remains sparse even though evidence selection is dynamic.

Before using a screenshot as truth-sensitive evidence, Chronicle must verify that the
returned pixels correspond to the requested frame and timestamp. Frame extraction tests
must cover variable and fractional frame rates and event-boundary seeks.

## Grounding and attribution

Multimodal memory must distinguish content roles:

- user-authored or user-spoken
- another person's speech or writing
- system/application output
- media dialogue or lyrics
- assistant/model-generated text
- OCR inference with no reliable author

Visible text is not automatically a fact about the user. In particular:

- assistant output visible in Codex or ChatGPT is not a user belief or completed action
- game chat is attributed to its visible speaker
- system-output audio is media content, not personal speech
- a displayed success message is evidence of application state, not necessarily of who
  caused it
- a user reaction can be retained only when its authorship is supported

The structured event should preserve these roles at the assertion level.

## Time semantics

Store canonical timestamps in UTC, but derive Daily paths, headings, and user-facing
grouping from the user's configured timezone. Preserve the original timezone or offset
used for presentation.

An event crossing midnight should be assigned according to explicit product policy, such
as start time, end time, or the local day containing most of its duration. It must not be
assigned accidentally by converting everything to a UTC date.

## Lifecycle and concurrency

- Collector restarts must reconcile saved state against the local frame cursor.
- Backend observations and events require explicit stale-open reconciliation.
- Ingestion and curation must not overwrite each other's newly appended samples.
- Revision claims need atomic compare-and-set semantics.
- A curation run operates on a stable revision; newly arrived evidence creates the next
  revision.
- Missing fingerprint predecessors and source gaps must be observable in metrics.

## Retention model

Chronicle should preserve four different retention tiers:

1. **Local raw evidence:** high volume, retained according to ScreenPipe policy.
2. **Remote signals and observations:** compact audit and retrieval pointers.
3. **Structured events:** queryable facts and outcomes with provenance.
4. **Curated vault memory:** sparse, durable knowledge and selected media.

Deleting or discarding one tier must not be confused with a decision at another tier.
For example, deleting a temporary preview does not discard the event, and declining a
vault note does not remove the structured match result.

## Concrete validation scenario

The audited AoE session is a useful acceptance test, not the architecture itself.

Given local frames containing:

- a match lobby or loading state
- long contextless gameplay
- a defeat result
- another lobby
- long contextless gameplay
- a victory result
- a switch into unrelated development work

Chronicle should:

- discover two match events
- request only bounded supporting evidence
- attach the correct result image to each match
- avoid merging the later development activity
- store the two structured outcomes
- optionally create one Daily rollup with a 1–1 session result
- retain attributed tactical advice only if policy considers it useful
- avoid storing raw chat, resource counters, and routine gameplay frames

The same test pattern should be repeated with unfamiliar synthetic domains so the system
cannot pass only through hard-coded game vocabulary.

## Acceptance criteria

- Unknown event types can be proposed and stored without a collector release.
- The collector operates without model credentials.
- One long visual context can produce multiple events.
- One event can span several applications or devices.
- A later outcome can revise or close an earlier event.
- Decisive evidence can bypass periodic sampling without uploading a frame stream.
- Preview selection can change when the event hypothesis changes.
- Every retained factual assertion has source provenance and confidence.
- User-authored, third-party, media, system, and assistant-generated content remain
  distinguishable.
- Event extraction remains useful even when vault curation writes nothing.
- Daily notes use the user's local timezone.
- Restarts and concurrent curation do not orphan observations or lose samples.
- Requested frame pixels match the corresponding source frame and OCR.
- Historical local evidence can be backfilled through bounded event-discovery jobs rather
  than bulk upload.

## Near-term implementation direction

1. Introduce the structured event ledger and separate it from observation curation.
2. Add a backend event-proposal job that can request bounded follow-up evidence.
3. Extend collector jobs to return before/after frames and bounded OCR histories.
4. Make evidence candidates revision-specific instead of observation-global.
5. Add stale-open reconciliation and atomic revision claims.
6. Use the user's timezone for vault presentation.
7. Fix and test ScreenPipe frame extraction at fractional frame rates.
8. Add optional extractor packs only after the generic proposal path works.
9. Backfill the audited period from local ScreenPipe as a bounded validation exercise.

The earlier screen-context-memory plan (local, in `docs/plans/`) remains useful for
sparse capture and observation transport, but its assumption that application context
is an adequate event boundary must be replaced by this
observation-to-event-to-memory model.
