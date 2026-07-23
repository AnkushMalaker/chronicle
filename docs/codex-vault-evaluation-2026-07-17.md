# Codex Vault Evaluation — 2026-07-17

## Question

Can Chronicle's Codex CLI memory executor build a useful vault from scratch, and how
does it compare with the existing direct tool-calling/API-agent vault?

## Experimental setup

- Replayed the same eight high-worthiness conversations used by the 2026-07-16 deep
  fidelity audit: four long technical/work conversations and four personal
  Hindi-English conversations.
- Source: `backends/advanced/data/transcript_categorization/transcripts.jsonl`.
- Baseline: the existing direct-agent vault at
  `backends/advanced/data/conversation_docs/69b80e5894aa9ec334a421c9`.
- Candidate: an empty scaffolded vault at
  `artifacts/memory-executor-eval/codex-audited-8`.
- Executor: Codex CLI authenticated through the ChatGPT subscription.
- Model: `gpt-5.6-terra`, reasoning effort `low`. The model and effort were explicitly
  pinned and recorded in `evaluation-manifest.json`; no global CLI default was used.
- Runs were sequential. The evaluator disabled Redis locking only inside its isolated
  one-process throwaway vault; production locking was unchanged.

The model received the transcript, vault schema, and correctness invariants. It chose
how to inspect and edit the vault itself. The prompt does not prescribe grep/ripgrep or
a host-selected file-reading plan.

## Execution result

All 8/8 conversations completed successfully with no reported Codex errors or
truncation.

| Measure | Result |
|---|---:|
| Total wall time | 888.5 s (14.8 min) |
| Per conversation | 46.6–291.1 s |
| Conversation notes | 8 |
| Person notes | 32 |
| Topic notes | 22 |
| Files in candidate vault, including scaffold | 68 |
| Candidate conversation-note bytes | 14,228 |
| Baseline conversation-note bytes | 15,543 |

The wide run-time and tool-count variation (3–5 shell command groups) shows that Codex
did not execute one fixed host-side recipe. It chose a materially broader entity pass
for the largest Verizon/Galileo conversation.

## Quality comparison

### Better than the direct-agent baseline

1. **Better late-transcript coverage.** The candidate captured the FDE/customer-account
   half of `5c0b2333`, JPMC/Morgan Stanley context, Vipul's workload-boundary discussion,
   and the DevOps/Grafana follow-up. Vipul's person note also records his prior DB ML
   engineering work and roughly five years of experience.
2. **Recovered a previously missed health fact.** `19f5a281` now records the advice to
   avoid synthetic folic acid because of a genetic marker.
3. **More useful people layer.** Named participants and mentioned people are promoted
   into notes rather than leaving almost all biographical information in conversation
   prose. Asif now has a note tied to the incorrect L4 email action item.
4. **More careful uncertainty in one known attribution trap.** `7620c7b4` says a flooded
   RabbitMQ queue was *suspected*, that the cause required verification, and that Ragu's
   production visibility was needed. This is better than presenting the hypothesis as
   an established cause.
5. **Semantic titles.** All eight titles are meaningful. The worst baseline raw-ASR
   titles (for example, “So, the chronicle. I forgot. Yeah,”) are gone.
6. **Conservative noisy-case handling.** The noisy `fd3f7f7d` note is short and does not
   invent named people or confident personal claims.

### Still weak or worse

1. **Important omissions remain.** `a4ed37ac` still omits Ankush's father not attending
   the wedding and the surrounding estrangement. `d0de4521` captures UPI, insurance,
   medication reminders, journaling, and the scam, but still omits the sickness/Prayal
   portion. The customer-escalation/anger dynamic around Asif's email in `ea282e40`
   remains flattened.
2. **Entity creation overshoots.** The fresh eight-note candidate created 32 People and
   22 Topic notes. Some are useful promotion; some are weak one-mention entities or ASR
   name variants (`Watsal`, `Shoy`) that need reconciliation before a full rebuild.
3. **Unresolved links remain.** Candidate conversation notes contain 213 links to 66
   distinct targets, with 18 occurrences / 11 distinct targets unresolved (`JPMC`,
   `Morgan Stanley`, `Pune`, `RabbitMQ`, languages, and several one-off things). The
   baseline eight notes contain fewer links (68) and 8 distinct unresolved targets.
   Codex greatly improves graph richness but does not yet satisfy graph closure.
4. **Possible over-specific action ownership still needs source-level grading.** The
   generated actions look plausible, but this pass did not independently label every
   action and attribution against transcript spans. Execution success is not a semantic
   correctness score.

## Verdict

At the cheapest suitable current Codex tier tested, Codex mode is already a better
*memory builder* than the direct-agent baseline on coverage, titles, people promotion,
and later-transcript recall. It is not ready for an unattended full rebuild because it
trades the old vault's under-coverage for a noisier, partially unresolved entity graph.

The right next iteration is not a larger model by default. First add deterministic
post-run graph validation (resolve or remove every emitted entity link), case/name
reconciliation, and a small semantic regression rubric for the known facts above. Then
run the identical harness with `gpt-5.6-terra` low and a larger model; because the
executor prompt specifies outcomes rather than reading technique, that comparison will
measure the models' own strategies fairly.

Official Codex guidance describes `gpt-5.6-terra` as the faster, lower-cost choice for
read-heavy scans and supporting-document processing, while `gpt-5.6` is the starting
point for demanding ambiguous multi-step agent work:
<https://learn.chatgpt.com/docs/models>.

## Reproduction

From `backends/advanced`:

```bash
uv run python scripts/evaluate_memory_executor.py \
  --executor codex \
  --model gpt-5.6-terra \
  --reasoning-effort low \
  --dataset data/transcript_categorization/transcripts.jsonl \
  --output ../../artifacts/memory-executor-eval/codex-audited-8
```

The output directory must be empty unless `--keep-output` is explicitly supplied.
