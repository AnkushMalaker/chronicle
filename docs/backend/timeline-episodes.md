# Semantic timeline episodes

Chronicle's Timeline is a revisioned semantic view of capture evidence. It does not use
ScreenPipe transport chunks, backend audio batches, or observation lifecycles as
user-visible boundaries.

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

The timeline scheduler runs every 30 minutes by default. This cadence makes a changed
day available promptly; it is not an episode boundary. Evidence-revision hashing avoids
a Codex call when nothing changed. The first tick after local midnight also reconciles
the completed previous day.

## Evidence and agent execution

For one IANA-local day, the evidence broker combines audio profiles, absolute-timestamped
ScreenPipe transcripts, observations, meeting markers, Immich candidates, images, and
explicit capture gaps. System output is attributed as `media_content`; microphone input
remains `uncertain` unless stronger speaker evidence is available.

Evidence is organized into overlapping 20-minute coverage windows by default. One
file-backed Codex run reads every window, keeps bounded intermediate notes, and returns
an open-vocabulary episode set. Windows guarantee traversal and may be merged across;
episodes may overlap. The backend rejects a complete result if it omits a window,
invents evidence, uses implausible timestamps, or leaves assertions ungrounded.

Publishing is generation-based. All episodes for a run are inserted before
`TimelineDay.active_run_id` changes, so a failed revision never exposes a partial day.
Older generations remain available for audit but are not returned by the default API.

## Conversations and memory

Continuous ScreenPipe input and output are capture evidence. They are transcribed for
timeline use but are created with `data_purpose=capture_evidence` and
`memory_excluded=true`; speaker, title/summary, memory, and conversation-complete plugin
jobs are skipped. The default Conversations list and search exclude them. Genuine
browser, wearable, uploaded, and live-recorded conversations are unchanged.

## API

- `GET /api/timeline/day?date=YYYY-MM-DD&timezone=Area/City`
- `POST /api/timeline/analyze`
- `GET /api/timeline/analysis/{run_id}`
- `GET /api/timeline/episodes/{episode_id}/thumbnail`
- `PUT /api/timeline/timezone`

`GET /api/device-input/timeline` remains the raw diagnostic endpoint.

## Configuration

`config/defaults.yml` contains `timeline` evidence-window and Codex settings. The
`cron_jobs.timeline_analysis` schedule defaults to `*/30 * * * *`. Codex quota is
checked before execution; exhausted runs enter `quota_deferred` and retain their retry
time and bounded diagnostic.

Configurable timeline zoom, PI execution, automatic vault promotion, and historical
generation retention policy are not implemented yet.
