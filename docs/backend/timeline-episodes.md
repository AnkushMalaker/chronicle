# Semantic timeline episodes

Chronicle's Timeline is a revisioned semantic view of capture evidence. It does not use
ScreenPipe transport chunks, backend audio batches, or observation lifecycles as
user-visible boundaries.

A ScreenPipe observation is deliberately a coarse, continuous application session. A
two-hour game window can remain one observation while the semantic timeline represents
it as one gaming session or several independently supported events. Sparse samples do
not justify invented sub-boundaries. Chronicle retains the observation's source, time
range, and local frame identifiers so a future bounded context request can obtain more
resolution from ScreenPipe without mirroring its frame database.

## Audio storage and cadence

ScreenPipe's approximately 30-second audio files are transport and retry units. The
backend continues to assemble them into bounded compute spans (normally up to 30
minutes, or up to two hours for a collector-supplied meeting interval). Before clearing
the staged media bytes, Chronicle stores one `AudioEvidenceSpan` for the assembled span.

Each span contains parallel 10-second arrays for:

- capture coverage;
- VAD speech fraction;
- general acoustic activity;
- RMS and peak level.

These arrays distinguish voice inactivity, acoustic quiet, and missing capture. The
evidence broker can slice or aggregate them without creating one MongoDB document per
10-second bucket. A loud instrumental soundtrack can therefore be non-speech but
acoustically active.

After the durable span is written, Chronicle deletes its processed `DeviceInputItem`
audio staging rows. The span's source-item list is also the replay tombstone, so a reset
collector checkpoint cannot reinsert an already compacted chunk. Non-audio observations
remain in `DeviceInputItem`.

The timeline scheduler runs every four hours by default (`cron_jobs.timeline_analysis`).
This cadence makes a changed day available promptly; it is not an episode boundary.
Every run regenerates the whole day from scratch, so a tighter cadence re-does the same
work and reshuffles episode ids. Evidence-revision hashing avoids a Codex call when
nothing changed. The first tick after local midnight also reconciles the completed
previous day.

## Evidence and agent execution

For one IANA-local day, the evidence broker combines audio profiles, absolute-timestamped
ScreenPipe transcripts, observations, meeting markers, Immich candidates, images, and
explicit capture gaps. System output is attributed as `media_content`; microphone input
remains `uncertain` unless stronger speaker evidence is available.

Evidence is organized into overlapping 60-minute coverage windows by default. One
file-backed Codex run reads every window, keeps bounded intermediate notes, and returns
an open-vocabulary episode set. Windows guarantee traversal and may be merged across;
episodes may overlap. The backend rejects a complete result if it omits a window,
invents evidence, uses implausible timestamps, or leaves assertions ungrounded.

### The workspace must stay small enough to read

Segmentation silently returned nothing on whole days for a long time. The cause was
workspace size, not the model: on one real day the agent was handed **19.6 MB across
113 files**. Two leaks, both invisible because `max_text_chars_per_window` — the only
size control — governed just 12% of the payload:

1. **`manifest.json` was written in full**, 7.3 MB, with every evidence item's
   untruncated excerpt. Only the per-window files were capped. It is now a day header
   (~330 bytes); the prompt directs the agent to `windows/` regardless.
2. **`metadata` was unbounded — 71% of every window file.** `_transcript_item` embedded
   a `segments` array duplicating the transcript with per-segment timestamps (119 KB for
   a single conversation); `_audio_item` embedded five per-10-second numeric series;
   observations carried `sample_fingerprints` and `frame_candidates`.

`write_workspace` now drops known-bulk keys and applies a 2000-char backstop to any
value not on that list. `_transcript_item` emits a speaker-attributed excerpt
(`daksh: …`) instead of a parallel segments blob — smaller *and* more useful, since it
is how the agent learns who spoke. Net: 19.6 MB → 9.4 MB, and the JSON specifically
12.5 MB → 2.3 MB.

`window_minutes` moved 20 → 60 (85 → 26 windows/day) with `max_text_chars_per_window`
raised to 60000 to compensate. Window size is a capacity knob, not a semantic one — the
prompt treats windows as coverage units that must never become episode boundaries.

**If full days stop resolving, measure the workspace before suspecting the model.**
Build one with `write_workspace` and check total bytes and the largest window file;
window *count* is a weak proxy, since payload size is what actually varies.

### A malformed episode is dropped, not fatal

Under `--output-schema` every agent chat message is schema-shaped, so the agent's own
planning narration is a *valid* episode object — runs were observed emitting
`{"kind": "task", "title": "Inspect Chronicle day inputs"}`. Validation used to raise on
the first ungrounded episode, discarding a whole day's good output over one such entry
(`episode 4 has no temporally overlapping evidence`).

Episodes that fall outside the day, cite unknown evidence, have no temporally
overlapping citation, or cannot be bounded to a positive interval are now dropped
individually, with parent indices remapped. If *nothing* survives, the run fails as
`TimelineIncompleteSegmentation` rather than publishing an empty day.

Relatedly, `--output-last-message` must never point at the file the prompt tells the
agent to write. It did (`timeline-result.json`), so Codex overwrote the finished result
with its last chat message. The agent's file is now authoritative and the last message
is only a fallback for a run that answered inline.

### An empty result is a failure, not an answer

If the agent returns no episodes *and* no unassigned intervals for a day that has
evidence, that is `TimelineIncompleteSegmentation` — a retryable failure. The run
retries once at the next reasoning effort up the ladder
(`none→low→medium→high`, terminating at `high`).

This restores a check that was removed once to tolerate incomplete output from a newly
configured model. Tolerating it converted every empty pass into a *successful* run that
published zero episodes and superseded a good generation, so days silently went blank.
Two independent guards now cover it: this one fails the run before publishing, and
`_guard_empty_generation` refuses to let a zero-episode generation supersede a populated
one even if the first is somehow bypassed.

### Why an interval is unassigned is derived, not asserted

An unassigned interval carries a `cause` of `no_capture` or `unexplained`, computed from
the manifest: an interval overlapped by evidence other than a `capture_gap` was captured
and merely not explained, and only that case reflects on segmentation.

The agent's `reason` prose is kept but not trusted for this. One run labelled a
three-hour recording blackout "No evidence supports a coherent episode in this gap",
which reads as a segmentation failure rather than a period when nothing was recorded.
The web UI lists the two causes separately for the same reason. Days analyzed before
this existed have no `cause` and are shown undivided rather than assigned one
retroactively.

The check is skipped when episodes are pinned — a day already accounted for by confirmed
episodes legitimately leaves the agent nothing to add. Partial coverage is still caught
by evidence accounting.

Publishing is generation-based. All episodes for a run are inserted before
`TimelineDay.active_run_id` changes, so a failed revision never exposes a partial day.
Older generations remain available for audit but are not returned by the default API.

## Open question: full regeneration vs. incremental append

Every run regenerates the whole day from scratch. The previous generation is passed to
the agent only as advisory revision context (`_existing_payload`); nothing accumulates,
and the agent is free to ignore it — which it largely does. The only additive path is a
human-confirmed episode, which is pinned and carried forward verbatim.

Two consequences, both unmeasured:

- **Output is non-deterministic.** The same day produced 8, then 10, then 11 episodes
  across consecutive runs, with different titles and boundaries. A refresh can change
  what your day "was".
- **`episode_id` churns every run.** Only `episode_key` survives, and only for confirmed
  episodes, so a link to `/timeline/{episode_id}` breaks on the next analysis.

**Suspected hazard with passing prior context: error propagation.** Handing the agent
the previous generation invites it to inherit that generation's mistakes — a
misattributed participant or a wrong boundary can be copied forward and then look
corroborated by its own repetition. Full regeneration at least re-derives from evidence
each time. Nobody has measured whether the revision context helps continuity more than
it entrenches error; it is currently supplied unconditionally.

**Worth benchmarking** before committing to either shape: full regeneration versus an
append/edit model that adds new observations onto the existing day.

### Score content per second, not segmentation

The obvious metric — how much two segmentations of the same day disagree — does not
work here, because **granularity is genuinely ambiguous**. Three consecutive Age of
Empires sessions and one "gaming evening" are both defensible readings of the same
evidence, so raw partition disagreement conflates real instability with a legitimate
choice, and an episode count is not evidence of anything on its own.

The primary metric is therefore **per-second content agreement**: label every second
with its kind and entities and compare those labels. Whether 19:15–19:52 is one episode
or three, every second is still `gaming / Age of Empires IV`, so the measure is
invariant to how the day is chunked. Granularity is then reported *separately* as a
diagnostic — episode count, and boundary precision versus recall, which localizes a
difference to over- or under-segmentation instead of scoring it as error.

The same metric serves both axes:

- **Stability** — agreement between two runs over identical evidence. No labels needed.
- **Accuracy** — agreement against a hand-labeled day.
- **Error persistence** — seed a wrong confirmed episode and check whether later runs
  inherit it. No labels needed.

Cost and latency follow from the structure and should not be the deciding axis alone.

### Labeled days must not reach the agent

Confirming an episode pins it, and a pinned interval is handed to the agent as one it
must not re-segment. A gold set built by confirming episodes would therefore be fed
back as the answer key, and any run scored against it would look near-perfect. A
benchmark run must pass no pinned intervals regardless of what is confirmed in the
database.

Correcting a generated day is also not the same as labeling one independently: it
invites accepting plausible boundaries that would never have been drawn from scratch,
which flatters the system. That bias is unmeasured.

## Confirm and pin

Episodes are regenerated per run with a fresh `episode_id`, so a human correction would
normally be erased by the next analysis. `episode_key` is the durable identity that
survives regeneration.

Editing an episode through `PATCH /api/timeline/episodes/{episode_id}` sets
`confirmed_at`, appends the edited field names to `confirmed_fields`, and moves `status`
to `confirmed`. On the next run those episodes are:

1. passed to the agent as pinned intervals it must not re-segment or mark unassigned,
2. treated as already accounted for by evidence validation, and
3. carried into the new generation verbatim — same `episode_key`, same human-authored
   fields — with their evidence refs refreshed from the new manifest where the cited
   evidence still exists.

A drafted episode that lands mostly inside a pinned interval anyway is dropped after
bound clamping, so one stretch of the day never shows two episodes.

**Pinning keys on `confirmed_at`, not `status`.** `status` defaulted to `"confirmed"`
before this existed, so thousands of never-touched episodes carry that value; keying on
it would freeze whole days against reanalysis. New episodes default to `provisional`.
The UI shows its confirmed badge on `confirmed_at` for the same reason.

## Recordings and memory

Continuous ScreenPipe input and output are capture evidence. They are transcribed for
timeline use but are created with `data_purpose=capture_evidence` and
`memory_excluded=true`; speaker recognition still runs (the agent cannot name who was
present without it), while title/summary, memory, and conversation-complete plugin jobs
are skipped. The default Recordings list and search exclude them — on
`data_purpose != "capture_evidence"`, not on transport — but the recordings themselves
stay fully openable, and an episode links to each one it cites. Genuine browser,
wearable, uploaded, and live-recorded recordings are unchanged.

### A conversational episode promotes the recordings it cites

Fencing all ScreenPipe audio out of search is right for ambient capture and wrong for
the subset that is an actual meeting: a standup or 1:1 captured this way was transcribed,
stored, and then unfindable. Meeting detection catches only the case where an
application takes the microphone, and the recorder's own `meetings` table does not exist
on Linux at all.

The segmentation agent is what can tell the two apart, because it already reads the
speaker-attributed transcripts. `AgentEpisode.conversational` is its answer, and it is
deliberately **factual** — did two or more people exchange speech — not a judgement about
whether the episode mattered. `salience` carries that, and what is worth *remembering*
stays the vault write agent's decision.

After publishing, `_promote_conversational_recordings` moves every cited recording of a
conversational episode from `capture_evidence` to `conversation`, clears
`memory_excluded`, and enqueues title/summary so it is not left untitled. It does **not**
enqueue memory: that comes from the day pass below.

Promotion is one-way and idempotent — it only ever moves a recording *out* of
capture evidence, and re-running finds nothing left to move. That matters because
regeneration is non-deterministic: a later run that no longer calls the episode
conversational must not re-hide a recording the user has already seen.

Cited ids are the union of the agent's `related_conversation_ids` and the
`conversation_id` recorded in each `evidence_refs[].metadata` at assembly time. The agent's
list can omit or invent one; the assembly-time half cannot. Only ids resolving to a real
capture-evidence recording are acted on, which discards anything invented.

### The day, not the conversation, is the memory unit

ScreenPipe audio is assembled into bounded compute spans — 30 minutes, or two hours given
a collector meeting interval — so a 45-minute standup without meeting detection is
*already* two recordings. Remembering per recording would inherit those arbitrary cuts
forever. An episode carries the semantic bounds instead, so `episode_memory`
(`services/timeline/memory.py`) records a whole settled day of episodes in one vault
write, and the recordings underneath are artifacts it cites.

The record lands in `Daily/<local_date>.md` via `add_day_memory`, not under
`Conversations/`, which stays one note per conversation. Durable People/Topic/Category
edits are unchanged. Conversational episodes carry the speaker-labelled transcripts of
their recordings, built with the same `build_memory_transcript` the conversation path
uses; every episode carries its assertions with `role` and `confidence`, which is what
keeps `media_content`, `application_state`, and `assistant_generated` claims from being
recorded as facts about the user.

**Only a settled day is written, and only once.** A day is eligible when it is in the
past, has an analysis, and its active run has been still for `settle_minutes`.
`TimelineDay.memory_state` is the latch: memory is written once per (user, local_date),
and a later re-analysis changes `active_run_id` without re-triggering a write. Writing on
publish instead would record the same stretch of the day repeatedly under different
boundaries, because the same day has been observed producing 8, then 10, then 11
episodes across consecutive runs.

`lookback_days` bounds how far back the pass looks, so enabling it on an existing
deployment records the recent days rather than rewriting months of vault at once. A day
that keeps failing settles into `skipped` with its diagnostic after `max_attempts` rather
than being retried on every tick forever. A digest over `max_digest_chars` sheds its
lowest-salience non-conversational episodes — never a conversational one — and logs
exactly which, so a shortened day is never mistaken for a complete one.

This replaced a per-observation Codex curation pass that wrote a Daily note per
observation. That pass was the "an observation is not an event" error in
[multimodal-memory.md](../multimodal-memory.md) made concrete: it split one activity
across many notes and could not see the day. Its frame-shortlist half is not missed
either — the segmentation workspace never contained image bytes (the agent nominates a
representative purely from an item's `image_filename`), and episode thumbnails are
sampled from an episode's own interval regardless of what any observation shortlisted.

An episode is the event; a recording is the artifact it cites. `TimelineEvidenceRef`
persists the assembly-time `metadata`, so an `audio_span` or `transcript` ref carries the
`conversation_id` that makes that link possible. The web UI opens each cited recording in
place on the episode page and deep-links to
`/recordings/{id}?start=&end=` in seconds from the recording's start.

## API

- `GET /api/timeline/day?date=YYYY-MM-DD&timezone=Area/City`
- `POST /api/timeline/analyze`
- `GET /api/timeline/analysis/{run_id}`
- `GET /api/timeline/episodes/{episode_id}`
- `PATCH /api/timeline/episodes/{episode_id}` — edit `title`, `summary`, `kind`,
  `entities`, `salience`, `started_at`, or `ended_at`; any edit confirms and pins the
  episode
- `POST /api/timeline/episodes/{episode_id}/split` — cut at `at`, which must fall
  strictly inside. Evidence is repartitioned by overlap, so each half cites only what
  it covers and a ref spanning the cut belongs to both; assertions whose evidence did
  not survive on a side are dropped. The tail gets a fresh `episode_key`.
- `POST /api/timeline/episodes/merge` — collapse `episode_ids` (same run, two or more)
  into the earliest, which keeps its `episode_key`; evidence, entities, and assertions
  are unioned
- `DELETE /api/timeline/episodes/{episode_id}` — remove from this generation. **Not a
  negative label**: nothing records that the interval should stay unexplained, so a
  later run may propose an episode there again.
- `GET /api/timeline/episodes/{episode_id}/thumbnail`
- `PUT /api/timeline/timezone`

`GET /api/device-input/timeline` remains the raw diagnostic endpoint.

## Configuration

`config/defaults.yml` contains `timeline` evidence-window and Codex settings, plus
`timeline.thumbnails.codex` for the frame-picking vision pass and `timeline.memory` for
the settled-day vault write. The `cron_jobs.timeline_analysis` schedule defaults to
`0 */4 * * *` and `cron_jobs.episode_memory` to `20 * * * *`. Codex quota is checked
before execution; exhausted runs enter `quota_deferred` and retain their retry time and
bounded diagnostic.

Configurable timeline zoom, PI execution, and historical generation retention policy are
not implemented yet.
