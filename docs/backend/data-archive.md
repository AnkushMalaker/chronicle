# Data Archive and Memory Rebuild

Chronicle can export its durable data to a checksummed `.chronicle` archive,
restore that archive, and reconstruct the Markdown memory vault from the active
conversation transcripts.

## What is archived

The archive contains:

- every MongoDB collection as a BSON stream, including users, conversations,
  transcript versions, annotations, chat data, and `audio_chunks`;
- the original Opus bytes and timing metadata stored in each audio chunk;
- `data/conversation_docs/`, `data/memory_md/`, and legacy
  `data/audio_chunks/` files;
- a versioned manifest with the document count, byte size, and SHA-256 digest
  of every archive member.

BSON is used instead of JSON so MongoDB ObjectIds, datetimes, binary audio, and
nested transcript data round-trip without conversion. Archives include password
hashes and personal data from the users collection. Store them as sensitive data.

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

During import, Chronicle first groups conversations by chunk structure, then decodes
only matching candidates and fingerprints their PCM samples. This detects the same
clip even when its Opus container bytes differ. If identical audio occurs more than
once in the archive, only the earliest conversation by `created_at` is imported; its
transcript versions are retained and the later conversation plus related audio
chunks, annotations, waveforms, and other conversation-scoped records are skipped.
Merge imports also compare against audio already in MongoDB, where the existing
conversation wins. Every skipped duplicate is written to the application log and
printed by the CLI with both conversation IDs.

## Restore an archive

Merge restore upserts documents by MongoDB `_id` and overlays filesystem files:

```bash
./chronicle-data.sh import /app/data/backups/before-upgrade.chronicle
```

Replace restore clears every collection and filesystem root represented by the
archive before restoring it:

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
  --replace --rebuild-from speakers --force
```

The `speakers` mode runs speaker recognition first and creates new active transcript
versions before starting memory reconstruction. It processes every non-deleted
conversation with an active transcript; `memory_excluded=true` conversations receive
speaker processing but remain excluded from memory. The `memory` mode starts directly
from the imported active transcripts.

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
./chronicle-data.sh rebuild-memory --user-id 507f1f77bcf86cd799439011 --force
```

The rebuild refuses to start when an existing queued, deferred, scheduled, or
running speaker or memory job targets any selected conversation. Syncthing
`.stfolder` and `.stignore` markers are retained while vault content is cleared.
