# Repository cleanup inventory

This document tracks the cleanup of the working tree started on 2026-07-17.

## Initial state

- Branch at inventory time: `dev` (matching `origin/dev`)
- Cleanup branch: `git-cleanup`
- Modified tracked files: 84
- Untracked files: approximately 1,304
- Untracked disk usage: approximately 25 GB
- Staged files at inventory time: none

## Categories

### Project work to review and commit

- Advanced backend features and tests
- Advanced backend web UI changes
- ASR service source, configuration, and experiments that are suitable for the repository
- Speaker-recognition service and web UI changes
- CI and test infrastructure

### Generated outputs likely needing repository ignore rules

- Model checkpoints, adapters, and optimizer state
- Fine-tuning output directories
- Evaluation outputs and experiment logs
- Model and dataset caches
- Coverage reports

These generated artifacts account for most of the untracked disk usage. Individual
source files inside experiment directories must be separated from generated outputs
before ignore rules are added.

### Potentially personal or sensitive material

- Root-level audio, screenshots, QR codes, transcripts, and notes
- `golden_data/`
- Conversation-vault evaluation artifacts under `artifacts/`
- Voice-cloning experiments and recordings
- Personal planning and research documents

Do not move these without explicit confirmation.

### Local-only files

- Environment-file backups
- Caddy and TTS configuration backups
- Local logs, debug data, and model caches

Use `.git/info/exclude` when a file is specific to this checkout and cannot be moved
under `untracked/`. Use repository `.gitignore` only for outputs routinely generated
for multiple contributors.

## Privacy review

The names `ankush`, `anushpa`, and `jahnvi` occur in both tracked and untracked
content. Tracked occurrences include examples, tests, documentation, and sample-vault
data. Untracked occurrences include evaluation artifacts, experiment logs, datasets,
research notes, and tests. The evaluation vault under `artifacts/` should be treated
as sensitive until reviewed.

PII already present in Git history is out of scope. Current tracked and proposed new
content must be reviewed before the cleanup branch is finalized.

## Rules for this cleanup

- Confirm before moving any file.
- Do not delete or overwrite personal artifacts during categorization.
- Commit project work in coherent groups.
- Keep generated, local-only, and potentially sensitive material out of project
  commits until explicitly classified.
- Do not add backward-compatibility code as part of cleanup.

## Progress

- [x] Initial read-only inventory
- [x] Create cleanup branch
- [x] Commit advanced backend project work (`4d52a0c1`)
- [x] Commit ASR service project work (`667f5716`)
- [x] Commit speaker-recognition project work (`43b25ae7`)
- [ ] Classify remaining untracked content
- [ ] Apply approved ignore and relocation decisions
- [ ] Clean PII from the current repository state

## Verification notes

- Repository pre-commit hooks passed for all three project commits. Black and isort
  reformatted newly added Python files before the successful commits.
- The broad advanced-backend pytest run could not complete because existing MongoDB
  persistence tests require unavailable local infrastructure.
- `backends/advanced/tests/test_vad_analysis.py` was deliberately left untracked. It
  imports `silence_gaps_from_regions`, but that implementation is absent from the
  working tree and Git history, so the test is currently incomplete.
- ASR checkpoints, optimizer state, adapter outputs, caches, and logs were not
  committed.
- Speaker-recognition debug data and environment backups were not committed.
