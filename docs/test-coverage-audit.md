# Test Coverage Audit

Audit date: 2026-07-15
Git baseline: `0e2eefa5` on `dev`
Scope: Git-tracked code is the reproducible baseline. Current untracked source and tests were
examined separately because they are not available to CI or a fresh contributor checkout.

## Executive summary

Chronicle has broad end-to-end scenario coverage, especially around backend endpoints, audio
streaming, queues, and conversations. It does not currently have a trustworthy repository-wide
code coverage number.

The best available measurement is for the advanced Python backend:

- The current working-tree pytest suite executed about **20.0% of tracked backend statements**
  (`4,475 / 22,321`). This is an exposure measurement, not a passing baseline: the run had 127
  passes, 14 failures, and 33 errors after two collection failures were excluded.
- The smaller, Git-tracked, locally collectable unit subset exercised **8% of the statements
  Coverage.py could discover in that run** and had 69 passes and 3 failures. Coverage discovery
  omitted unimported namespace-package directories, so 8% is an upper bound rather than a full
  backend denominator.
- The backend's most important orchestration code is the least directly tested area: workers had
  **8.4%** measured coverage and controllers had **11.2%** in the broader working-tree run.
- All TypeScript/JavaScript products have **no configured test runner and no direct test files**:
  the main web UI, mobile app, speaker-recognition UI, and vault graph UI.
- CI runs Robot tests and one speaker-recognition integration test, but it does **not run backend,
  root-tooling, or ASR pytest suites**, and it publishes no line or branch coverage.

The project therefore has meaningful workflow protection, but weak feedback on which branches and
failure paths are exercised. Robot's documented "70% coverage" means percentage of Robot tests
selected, not percentage of application code covered.

## Inventory

### Test suites

| Surface | Current tests | Audit result |
| --- | --- | --- |
| Advanced backend | 7 tracked top-level pytest files; 12 more top-level files are currently untracked | Default collection fails; no pytest CI job |
| Cross-repo Robot suite | 232 tests across 34 suites | 118 endpoint, 64 integration, 28 ASR, 17 configuration, 4 infrastructure, 1 browser |
| Root setup/lifecycle tooling | 5 pytest files, about 80 test functions | Collection fails in 2 files; remaining run had 46 pass / 10 fail |
| ASR services | 4 pytest files, 63 collected cases | 49 pass / 9 skip / 2 fail / 3 environment errors |
| Speaker recognition | 1 large pytest integration scenario plus 2 ad hoc scripts | Secret-, model-, and Docker-dependent CI only |
| Frontends | No test files or test scripts | Build/lint only; no measured coverage |
| TTS and most plugins/extras | No direct automated tests | Some behavior is reached indirectly by Robot tests |

There are 232 Robot cases, but the normal no-API lane selects only 166 after tag exclusions. The
suite contains 17 tests with unconditional `Skip`, including eight mobile placeholders and five SDK
placeholders. These should not be counted as implemented coverage.

### Advanced backend line coverage

This table comes from the broader current-working-tree pytest run. Failing tests still execute
lines, so the numbers indicate code reached by tests, not verified behavior.

| Backend area | Statements | Covered | Coverage |
| --- | ---: | ---: | ---: |
| Models | 711 | 479 | 67.4% |
| Plugins framework | 453 | 136 | 30.0% |
| Routers | 3,598 | 943 | 26.2% |
| Utilities | 1,147 | 262 | 22.8% |
| Services | 5,490 | 1,207 | 22.0% |
| Package root | 2,838 | 625 | 22.0% |
| Observability | 246 | 51 | 20.7% |
| Clients | 260 | 47 | 18.1% |
| Controllers | 4,169 | 467 | 11.2% |
| Workers | 3,532 | 297 | 8.4% |

Across the measured backend, 34 files had zero executed lines and 46 more were below 20%. The
largest high-risk gaps include:

- `app_factory.py`: 289 statements, 0%
- `task_manager.py`: 192 statements, 0%
- memory agent edit/tool modules: 611 combined statements, 0%
- worker orchestrator modules: 465 combined statements, 0%
- `system_controller.py`: 1,103 statements, 10.0%
- `websocket_controller.py`: 712 statements, 9.3%
- `conversation_jobs.py`: 715 statements, 12.2%
- `transcription_jobs.py`: 561 statements, 8.9%
- `speaker_recognition_client.py`: 624 statements, 5.9%

The root management tests measured 18% across five imported modules. `updates.py` reached 93%, but
`services.py`, `config_manager.py`, `discovery.py`, and `setup_utils.py` were between 10% and 20%.
This is not a whole-root percentage because two configured modules were never imported.

## Suite health findings

### 1. Test execution is not reproducible from a clean checkout

- Advanced backend collection references deleted or changed APIs in `test_obsidian_service.py` and
  `test_vad_analysis.py`.
- Root tests reference removed `merge_configs`, `read_config_yml`, and service lifecycle APIs.
- The ASR default suite starts Docker from an otherwise unit-oriented `pytest tests` command; Docker
  absence becomes an error instead of an integration skip. Two independent ROCm Dockerfile checks
  also currently fail.
- Robot dry-run found 16 failures in configuration suites because `ruamel-yaml` is imported but is
  absent from `tests/test-requirements.txt`.
- The browser suite requires `rfbrowser init`, but that setup is absent from the test runner and CI.

### 2. Test location does not consistently describe test type

- `backends/advanced/tests/` mixes pure units, MongoDB integration tests, and manual graph-validation
  scripts. The untracked graph scripts are automatically collected by pytest but expect command-line
  arguments as fixtures.
- Root `tests/unit/` mixes root lifecycle scripts with advanced-backend configuration behavior.
- Robot `endpoints/` tests are full-stack HTTP contract/integration tests, not unit endpoint tests.
- Robot `integration/` includes genuine end-to-end flows, SDK contract tests, and placeholder mobile
  scenarios.
- Hardware-, secret-, browser-, and container-dependent tests are separated mainly by tags, but tag
  hygiene is not enforced.

### 3. Tags and documentation have drifted

- `tests/tags.md` says there are 15 approved tags, later says 14, and finally asks whether one of 11
  tags can be used.
- Ten tests in `client_queue_tests.robot` use prohibited tags such as `positive`, `negative`,
  `security`, `client`, `jobs`, and `integration`.
- Eighteen Robot tests have no tags.
- Only four of nine SDK tests carry the `sdk` tag. The other five are placeholder skips that remain in
  default selection.
- `tests/README.md` and `AGENTS.md` document nonexistent Make targets including `test-all`,
  `test-endpoints`, `test-integration`, and `test-infra`; the actual targets are `all`, `endpoints`,
  `integration`, and `infra`.

### 4. CI enforces workflows, not code coverage

The required OSS-friendly PR lane runs 166 no-secret Robot cases. That is valuable, but no workflow
runs the fast Python tests or any frontend tests. There is no `.coveragerc`, branch coverage setting,
coverage artifact, combined coverage job, patch coverage check, or minimum threshold.

Path filters also mean changes to root lifecycle tooling, ASR, TTS, plugins, and frontends do not
trigger the main Robot workflow unless they touch its listed backend/test paths.

## Proposed ownership model

Tests should live with the component whose code they primarily validate. The repository-level
`tests/` directory should own cross-component acceptance behavior only.

| Type | Location | Allowed dependencies | PR policy |
| --- | --- | --- | --- |
| Unit | `<component>/tests/unit/` | No network, Docker, database, secrets, or model downloads | Required; seconds |
| Component | `<component>/tests/component/` | In-process app plus fakes/in-memory stores | Required; minutes |
| Integration | `<component>/tests/integration/` | Real database/service containers | Required where CPU/no-secret |
| Contract | `tests/contract/` | Black-box API against composed Chronicle | Required no-secret subset |
| End to end | `tests/e2e/` | Multiple services and full user workflow | Required smoke subset; full scheduled |
| Environment | `tests/environment/{browser,gpu,hardware,secrets}/` | Explicit specialized runner | Optional/scheduled unless relevant |

Use pytest markers matching execution requirements, not business domains:
`unit`, `component`, `integration`, `docker`, `gpu`, `external_api`, and `slow`. Keep Robot business
tags for selecting product behavior, but validate them against one canonical allowlist in CI.

## Recommended rollout

### Remediation started

The first infrastructure tranche was implemented after this baseline was recorded:

- Root tooling, advanced backend, and ASR now have branch-coverage configuration and a dedicated
  Python CI workflow that uploads XML and HTML reports.
- Fast backend and ASR lanes explicitly exclude their MongoDB and Parakeet container integration
  modules. Manual graph-validation scripts are no longer eligible for default pytest collection.
- Robot tags now have a machine-readable allowlist and validation command. Invalid legacy tags were
  mapped to the existing taxonomy, and all SDK placeholders inherit the `sdk` execution tag.
- The 17 container-independent configuration tests are part of `make all`; they pass locally.
- The ASR fast lane now produces an **11.3% branch-coverage baseline** while retaining its two known
  failing Dockerfile assertions for the parallel test-repair work.

Coverage thresholds remain intentionally disabled until the known collection and assertion failures
are repaired. The new Python CI jobs will surface those failures rather than hiding them.

### Phase 0: make the existing signal trustworthy

1. Add separate CI jobs for root pytest, advanced-backend unit pytest, and ASR unit pytest.
2. Move or exclude manual graph-validation scripts so default pytest cannot collect them.
3. Split Docker/Mongo/GPU tests from unit commands and make missing prerequisites explicit skips.
4. Repair or remove tests for deleted APIs; do not retain compatibility code solely to satisfy stale
   tests.
5. Fix Robot dependencies, browser setup/selection, placeholder selection, tags, Make targets, and
   documentation.

### Phase 1: establish coverage without blocking cleanup

1. Configure line and branch coverage per Python component and publish XML/HTML artifacts.
2. Run the backend process under Coverage.py in Robot containers, save parallel data files, and
   combine them with pytest coverage. Report pytest and Robot flags separately as well as combined.
3. Add Vitest and React Testing Library to both Vite UIs; add the Expo-supported Jest/React Native
   Testing Library stack to the mobile app.
4. Record the baseline by component. Initially enforce only: green tests, no reduction in total
   coverage, and about 80% changed-line coverage. Do not impose an arbitrary 80% repository-wide
   target on legacy code.

### Phase 2: cover failure-prone boundaries

Prioritize tests around conversation lifecycle, transcription/audio persistence, worker retries and
idempotency, task cleanup, memory-agent edits, authentication/data isolation, and plugin failure
containment. For frontends, prioritize auth/token state, API error states, reconnect behavior, audio
session state, and destructive actions before snapshot-heavy presentation tests.

### Phase 3: ratchet by subsystem

Once the suites are green and stable, set modest per-component floors and raise them gradually.
Coverage should remain a discovery tool: mutation testing or focused fault injection on lifecycle
and worker code will reveal weak assertions that line coverage alone cannot detect.

## Definition of done for the cleanup

- One documented command runs every fast, no-secret test from a clean checkout.
- Every test has one owning component and one execution class.
- No placeholders are reported as passing coverage.
- All PRs run Python unit/component tests, frontend tests, and the no-secret contract smoke suite.
- Coverage is reported per component and combined for the advanced backend.
- Changed code cannot reduce coverage, while legacy coverage increases through a ratchet rather than
  a one-time repository-wide threshold.
