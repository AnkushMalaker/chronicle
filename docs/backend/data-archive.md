# Data Archive and Memory Rebuild

Chronicle can export its durable data to a checksummed `.chronicle` archive,
restore that archive, and reconstruct the Markdown memory vault from the active
conversation transcripts.

## What is archived

The archive contains:

- every MongoDB collection as a BSON stream, including users, conversations,
  transcript versions, annotations, chat data, and `audio_chunks`;
- the original Opus bytes and timing metadata stored in each audio chunk;
- `data/conversation_docs/` and legacy `data/audio_chunks/` files;
- a versioned manifest with the document count, byte size, and SHA-256 digest
  of every archive member.

BSON is used instead of JSON so MongoDB ObjectIds, datetimes, binary audio, and
nested transcript data round-trip without conversion. Archives include password
hashes and personal data from the users collection. Store them as sensitive data.

Mongo-backed screenshots and other `DeviceInputItem` media are included with their
documents. Filesystem-backed inference artifacts, Pi operating memory, and the canonical
vault are included from the data directory.

## Full and incremental archives

An export without a base is self-contained. Supplying one or more verified
`--base-archive` files creates a true incremental snapshot:

- unchanged `audio_chunks` already present in the base state are omitted by canonical
  BSON SHA-256, so their compressed audio bytes are not copied again;
- unchanged documents in every other MongoDB collection are omitted by the same digest;
- unchanged data-directory files are omitted by path, size, and SHA-256; and
- documents/files removed since the base are represented by tombstones.

Changed documents are stored in full. Thus a chunk whose quarantine/deletion metadata
changes is stored once in the delta even though its `_id` and audio bytes are stable;
an unchanged chunk is omitted entirely. Likewise, a changed Mongo document containing
image bytes is stored again, while an unchanged screenshot document is not. The manifest
carries cumulative document/file digest indexes, omitted IDs/paths, tombstones, and
checksummed base references; it does not copy unchanged base bytes.

```bash
# Self-contained baseline
./chronicle-data.sh export /app/data/backups/base.chronicle

# Delta; unchanged audio, Mongo documents, vault files, and artifacts are omitted
./chronicle-data.sh export /app/data/backups/delta-1.chronicle \
  --base-archive /app/data/backups/base.chronicle

./chronicle-data.sh verify /app/data/backups/base.chronicle
./chronicle-data.sh verify /app/data/backups/delta-1.chronicle
```

Keep the base chain: a delta is intentionally not another full copy.

## Maintenance window

Export is not a transactional MongoDB snapshot. For a consistent archive, stop
new device ingestion and the services that write data while running maintenance:

```bash
cd backends/advanced
docker compose stop chronicle-backend workers annotation-cron
./chronicle-data.sh export
docker compose start chronicle-backend workers annotation-cron
```

`chronicle-data.sh` uses a one-off Compose container, so the backend service may
remain stopped. Set `CONTAINER_ENGINE=podman` or `COMPOSE_CMD=podman-compose` for
a Podman deployment.

## Export and verify

With no output argument, archives are written under `data/backups/`:

```bash
./chronicle-data.sh export
./chronicle-data.sh export /app/data/backups/before-upgrade.chronicle
./chronicle-data.sh verify /app/data/backups/before-upgrade.chronicle
```

The exporter writes to a partial file and renames it only after completion. The
importer verifies the complete manifest and every checksum before changing MongoDB
or filesystem data.

## Back up only the Markdown vault

Create a small dated vault snapshot without exporting MongoDB or source audio:

```bash
./chronicle-data.sh backup-vault \
  --description "reingest after speaker fix"
```

Use `--user-id <id>` one or more times to limit the snapshot. The resulting
`data/backups/memory_vault_<UTC timestamp>.tar.gz` contains a `manifest.json` with the
description, creation time, selected user IDs, and the byte size and SHA-256 digest of
every file. This command only reads the live vault; it does not clear or rebuild it.

Merge restore upserts ordinary documents by `_id`. Audio identity is stricter: the
pair `(capture_session_id, sequence)` must resolve to the same immutable chunk `_id` in
the archive and target database. A conflict aborts instead of choosing one copy.

## Restore an archive

Merge restore upserts documents by MongoDB `_id` and overlays filesystem files:

```bash
./chronicle-data.sh import /app/data/backups/before-upgrade.chronicle
```

For an incremental chain, restore the verified base(s) in chronological order and then
apply the delta using merge restore. Before any delta mutation, Chronicle verifies that
every omitted audio chunk and Mongo document exists, that omitted documents still have
the expected digest, and that every omitted filesystem file still has the expected size
and SHA-256. A missing or changed base fails before mutation. Changed/new data is then
upserted and deletion tombstones are applied.

Incremental archives cannot be restored with `--replace`, because clearing the target
would destroy the base state the delta omits. Replace restore is available only for a
self-contained archive and clears every represented collection/filesystem root first:

```bash
./chronicle-data.sh import /app/data/backups/before-upgrade.chronicle \
  --replace --force
```

Use `--database-only` to leave the current vault and legacy filesystem audio
untouched.

## Restore and rebuild derived data

`--rebuild-from` imports the durable MongoDB records, deliberately skips the
archived `memory_audit` collection and vault files, clears current derived memory
state for all users, and queues active transcripts from the selected stage:

```bash
./chronicle-data.sh import /app/data/backups/before-upgrade.chronicle \
  --replace --rebuild-from memory --force

./chronicle-data.sh import /app/data/backups/before-upgrade.chronicle \
  --replace --rebuild-from days --force
```

### Choosing a stage

The stages are ordered by how far back they replay. Pick the earliest thing that
actually changed — each step back costs a full pass over the corpus.

| Stage | Replays | Use when |
|---|---|---|
| `memory` | per-conversation vault writes from the imported active transcripts | only the memory prompt or vault format changed |
| `speakers` | diarization + identification, then memory | enrollment or the speaker model changed |
| `days` | episode boundaries + the day vault write, over the **existing** speaker layer | the segmentation agent, day prompt, or episode-note format changed |
| `timeline` | diarization **and** boundaries **and** the day write | audio bounds changed — a re-bound, silence trim, merge or split |

`days` and `timeline` are the two that re-decide boundaries. Both delete existing
`timeline_analysis_runs`, `timeline_days`, and `timeline_episodes` first: a surviving
`TimelineDay` carries the write-once `memory_state` latch that would skip it, and a
surviving episode is offered back to the agent as prior art, so it reproduces the
boundaries the run exists to replace. Both enqueue **no** per-conversation memory jobs
— the day pass is the whole vault write, and running both would record the same audio
twice under the boundaries being replaced.

The difference is diarization, and it is the expensive half. `timeline` resets every
conversation to its ASR layer and fans out one speaker job per recording; on a corpus
of a few hundred recordings that is hours of GPU before the day chain can even start.
`days` keeps the speaker layer that is already active, which is the transcript the day
pass wants to read anyway — resetting to ASR would make it segment text with no
speakers in it. So reach for `timeline` only when the audio itself moved.

Within a boundary stage, speaker jobs fan out (they write only their own
conversation's transcript, so nothing orders them) while days run serially, because
each day's write takes that user's vault lock.

The `speakers` mode processes every non-deleted conversation with an active
transcript; `memory_excluded=true` conversations receive speaker processing but remain
excluded from memory. The `memory` mode starts directly from the imported active
transcripts.

Transcript-only conversations that have no stored audio chunks are reported and
skipped by the speaker stage. They still participate in memory reconstruction from
their imported active transcript.

Speaker and memory jobs are ordered chronologically within each user through RQ
dependencies. A failed speaker conversation is logged and skipped without blocking
later conversations; its memory is rebuilt from the unchanged active transcript.
Different users can rebuild in parallel. A copy of the old vault is written to
`data/backups/memory_vault_<timestamp>.tar.gz` before it is cleared; pass
`--no-vault-backup` to disable that copy.

Start the workers after the import command queues the replay chains:

```bash
docker compose start workers chronicle-backend annotation-cron
```

## Rebuild memory from current data

The same clean rebuild can run without importing an archive:

```bash
./chronicle-data.sh rebuild-memory --dry-run
./chronicle-data.sh rebuild-memory --force
./chronicle-data.sh rebuild-memory --rebuild-from speakers --force
./chronicle-data.sh rebuild-memory --rebuild-from days --force
./chronicle-data.sh rebuild-memory --user-id 507f1f77bcf86cd799439011 --force
```

If the vault is shared by a bidirectional filesystem sync service, pause that service
before a clean rebuild and resume it only after the finisher's whole-vault structural
check passes. Otherwise a peer can race the clear/rewrite window and reintroduce stale
root notes or old episode files even though the memory agent itself obeyed the current
schema. Chronicle preserves `.stignore`/`.stfolder` markers while clearing content.

The rebuild refuses to start when an existing queued, deferred, scheduled, or
running speaker or memory job targets any selected conversation. Syncthing
`.stfolder` and `.stignore` markers are retained while vault content is cleared.
