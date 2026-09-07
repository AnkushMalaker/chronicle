# Memory executor benchmark

`evaluate_memory_executor.py` replays JSONL transcript cases into a new, isolated
Chronicle vault. It supports the `direct`, `pi`, and `codex` writers without MongoDB,
Redis, queues, or a live user vault. `score_memory_executor.py` compares completed run
manifests using structural measurements only.

## Dataset contract

The default JSONL fields match Chronicle's existing transcript audit export:

```json
{"conversation_id":"case-001","created_at":"2026-08-01T10:00:00+00:00","transcript":"Speaker 0: A synthetic example.","duration_s":30,"title":"Synthetic case","guidance":"Optional exact guidance."}
```

`conversation_id`, `created_at`, and `transcript` must be non-empty strings. Guidance,
title, and duration are optional. Alternate field names have explicit CLI flags; the
harness does not guess aliases. A prefix must uniquely match one source ID. Repeated
prefixes define replay order, which matters because cases accumulate in one vault.

The transcript, date, and guidance strings are passed to the agent without stripping or
rewriting. Duration seconds are divided by 60 because the agent API accepts minutes.
The manifest stores input hashes, lengths, and the dataset line rather than duplicating
raw transcript/guidance text. Dataset position is audit context and is excluded from the
comparison fingerprint, so moving unrelated JSONL rows does not make identical selected
inputs incomparable. Agent completion summaries are likewise stored only as a hash and
character count because they can repeat personal source facts. The vault is the private
artifact for semantic inspection.

## Run

From `backend`, with the normal runtime configuration pointing at the model
being evaluated:

```bash
uv run python scripts/evaluate_memory_executor.py \
  --executor direct \
  --dataset /data/transcripts.jsonl \
  --source-id-prefix case-001 \
  --source-id-prefix case-002 \
  --output /tmp/chronicle-memory/direct-qwen

uv run python scripts/evaluate_memory_executor.py \
  --executor pi \
  --dataset /data/transcripts.jsonl \
  --source-id-prefix case-001 \
  --source-id-prefix case-002 \
  --output /tmp/chronicle-memory/pi-qwen
```

Repeat the flag for the locally selected, unique case prefixes in your audit set. Do not
publish stable IDs from a personal dataset. Use `--all` only for a dataset intended to
be replayed and disclosed completely.

Each output directory must be new or empty and receives:

```text
<output>/
  .gitignore
  manifest.json
  vault/
```

The harness forces an owner-only process umask, normalizes every output directory to
`0700` and every regular artifact to `0600`, and places a deny-all `.gitignore`
sentinel in the run root. This protects agent-created notes even when the host's normal
umask is group-readable and prevents a run made inside the checkout from being picked
up by a casual `git add`. Keep the run on an encrypted/private filesystem as well; a
forced Git add or a privileged local user can still bypass those safeguards.

The manifest is checkpointed after every case. The evaluator runs exactly one attempt
with the selected executor, canonicalizes a valid primary conversation note, and writes
Chronicle's deterministic source-preserving fallback otherwise. It does not invoke the
configured recovery backend, so a Pi/Codex result cannot silently contain another
agent's retry. The Codex quota guard is disabled for benchmark runs so an account-budget
decision cannot turn a Codex measurement into an incomplete attempt. In production,
Chronicle routes that incomplete result through the explicitly configured recovery
backend or deterministic source fallback; Codex never selects another backend itself.
A nonzero exit means at least one primary attempt failed its structural checks; the
manifest and remaining cases are still produced.

Invariant scans are cumulative because all cases share one evolving vault. Each run
therefore records both the current full issue set and `introduced_issues`/`resolved_issues`;
the scorer counts newly introduced issues instead of repeatedly charging later cases for
an earlier malformed note.

For Codex only, `--model` and `--reasoning-effort` override the CLI settings. Runtime
provenance records the effective Codex model after those overrides. Direct records the
resolved `memory_write` operation model and call parameters. Pi records its independently
resolved registry-model override and upstream model identity, plus effective context,
output limit, thinking mode, temperature, timeout, and compatibility settings. API keys
and model URLs are deliberately excluded.

## Compare

The first manifest is the baseline and all manifests must contain the same source order
and exact input fingerprints:

```bash
uv run python scripts/score_memory_executor.py \
  /tmp/chronicle-memory/direct-qwen/manifest.json \
  /tmp/chronicle-memory/pi-qwen/manifest.json

uv run python scripts/score_memory_executor.py --format json \
  /tmp/chronicle-memory/direct-qwen/manifest.json \
  /tmp/chronicle-memory/pi-qwen/manifest.json
```

The scorer is read-only. It reports completion, canonical/fallback and error rates,
latency, rounds, tool calls, reported token usage, and captured vault invariants. It does
not label fact extraction or answer quality: semantic scoring requires separately curated
gold claims and evidence.

The output vault can contain source transcript text when a deterministic fallback is
needed, and model summaries/errors may also be sensitive. Manifests retain source IDs,
note paths, errors, absolute local paths, and host metadata in plaintext even though
transcripts and summaries are hashed there. Owner-only modes and the sentinel reduce
accidental disclosure, but keep datasets and run outputs outside the repository and do
not commit them.

The resolved provider receives the full transcript and any note/tool content used during
the run. Verify `memory_write` and Pi/Codex settings point at the intended local or
approved service before using personal data. For a hosted GPU experiment, upload only a
locally selected minimum JSONL subset, download the private artifacts, and destroy the
remote instance and persistent volume afterward; pausing leaves the data behind.

## Focused tests

```bash
uv run pytest scripts/tests/test_memory_executor_benchmark.py
```
