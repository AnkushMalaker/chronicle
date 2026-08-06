# Memory retrieval benchmark

`evaluate_memory_retrieval.py` runs Chronicle's `direct` or `pi` retrieval path over an
ephemeral copy of an existing, fixed source vault. The configured agent never receives
the source path. It records answers, referenced note paths, errors, usage, rounds, and
latency in a JSON manifest.

The harness fingerprints the source, creates a private temporary copy, and fingerprints
both trees throughout the run. It aborts and returns nonzero if either tree changes. The
temporary copy is removed when the run unwinds through normal Python cleanup; the source is
never restored because the benchmark never writes to it. A concurrently changing source
invalidates the run rather than producing evidence from an inconsistent snapshot. After an
uncatchable kill or machine crash, remove any `.chronicle-memory-retrieval-*` directory left
inside the private output directory.

## Question set

Use a JSON array, a JSON object containing a `questions` array, or one object per line in
JSONL:

```json
{
  "questions": [
    {"id": "project-owner", "question": "Who owns the Atlas follow-up?"},
    {"id": "latest-decision", "question": "What was the latest Atlas decision?", "vault_summary": "Optional exact per-question context."}
  ]
}
```

IDs must be unique. Question and vault-summary strings are passed without rewriting.
Free-form answers are retained in the manifest; referenced note contents are represented
by hashes and lengths rather than duplicated. The harness does not assign semantic
correctness labels.

## Run direct and Pi

From `backends/advanced`, with the normal model configuration available:

```bash
install -d -m 700 /tmp/memory-bench

uv run python scripts/evaluate_memory_retrieval.py \
  --executor direct \
  --vault /data/fixed-vault \
  --questions /data/retrieval-questions.jsonl \
  --output /tmp/memory-bench/retrieval-direct.json

uv run python scripts/evaluate_memory_retrieval.py \
  --executor pi \
  --vault /data/fixed-vault \
  --questions /data/retrieval-questions.jsonl \
  --output /tmp/memory-bench/retrieval-pi.json
```

`--max-rounds` defaults to 6. `--vault-summary` supplies common learned context; a row's
`vault_summary` overrides it. Output must be a new path outside the source vault, and its
parent directory must have mode `0700`; a newly created parent is secured automatically.
Manifests and their atomic temporary files use mode `0600`, while the ephemeral workspace,
copied directories, and copied files use `0700`, `0700`, and `0600` respectively. A
nonzero exit means a question failed, returned no final answer, the private copy changed,
or the source changed while the benchmark was running.

The benchmark counts Chronicle's terminal failure sentinels as unanswered, even though
they are non-empty strings. The production search path requires `rg`; verify
`command -v rg` in a bare-host benchmark environment. Chronicle's backend images already
install it. A resumed hosted container may have lost system packages even though files
under its home directory persisted, so rerun environment setup after every resume.

Six rounds is a cap on tool-using turns. Direct also admits at most four calls per
configured round (24 calls by default), including calls emitted together in one model
turn. Once either cap is reached, Direct makes one fresh logical completion with tools
disabled from a compact evidence-only request built from notes it already read; it does
not replay the tool transcript. The serialized evidence JSON is capped at 16,000 UTF-8
bytes and 24 notes so it leaves conservative room in a 32K context. The answer is accepted
only when the provider reports a clean `stop` with no tool calls.

Pi may start one additional isolated process with no extension or built-in tools when the
first phase already read notes. That process can only synthesize from the supplied evidence
or abstain; it cannot search or mutate the vault. Both normal retrieval prompts and final
synthesis carry a code-owned invariant, appended after any registry prompt, that classifies
vault summaries, notes, and tool results as untrusted data rather than instructions.

If the output is inside a Git worktree, an existing ignore rule must cover the manifest;
the harness refuses an unignored path and does not modify the repository's `.gitignore`.
Prefer an output directory outside the checkout, as in the examples above.

Runtime provenance records the effective direct `memory_search` model and API call
parameters. For Pi it records the independently resolved Chronicle model override,
upstream model identity, provider, context and output limits, thinking/reasoning mode,
effective temperature, timeout, and compatibility settings. API keys and raw endpoint
URLs are deliberately excluded from the manifest.

Manifests are raw evidence for human or separately curated evaluation. Do not commit
personal vaults, question sets, answers, or manifests. Questions, answers, note paths,
errors, and absolute local paths remain plaintext in the manifest; file permissions are a
local safeguard, not anonymization.

The configured model provider receives the question and any note content returned by the
retrieval tools. Before using a personal vault, verify the resolved `memory_search` model
and endpoint are the intended local or approved service. For a hosted GPU experiment,
upload only the minimum vault/question subset, download the private manifest, and destroy
the remote instance or persistent volume after the experiment; pausing leaves data behind.

## Focused tests

```bash
uv run pytest scripts/tests/test_memory_retrieval_benchmark.py
```
