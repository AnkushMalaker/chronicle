# Screen memory lab

A harness for figuring out how to turn a ScreenPipe capture archive into events
and memories, and for keeping that answer honest once it exists.

It reads a real archive read-only, runs extraction pipelines over it, and scores
them against ground truth that was verified by hand. Results and analysis live in
[`docs/research/screen-memory/04-prototype-results.md`](../../docs/research/screen-memory/04-prototype-results.md);
the plan that came out of them is
[`docs/plans/screen-event-extraction.md`](../../docs/plans/screen-event-extraction.md).

## Setup

```bash
cd extras/screen-memory-lab
uv sync
printf 'OPENAI_API_KEY=sk-...\n' > .env      # gitignored
```

The archive location defaults to `~/.screenpipe` and can be pointed elsewhere with
`SCREENPIPE_DIR`. Nothing in this package writes to the archive.

## Looking at an archive

```bash
uv run python -m lab.survey span
uv run python -m lab.survey digest   2026-07-24T14:00 2026-07-25T01:00 --bucket 10
uv run python -m lab.survey anchors  2026-07-24T14:00 2026-07-25T01:00
uv run python -m lab.survey text     2026-07-24T15:27 2026-07-24T15:31
uv run python -m lab.survey frames   7171 7172 --image
```

`digest` is the compact index a backend can hold for a whole day — about 7,000
tokens for eleven hours, a 272× reduction of the captured text. `frames --image`
extracts pixels by decoded frame index, which is the only way to get a frame whose
image matches its stored OCR.

## Running the pipelines

```bash
uv run python run_lab.py --pipelines p1,p2,p3,p4,p5,p6
uv run python run_lab.py --pipelines p6 --model gpt-5.4 --tag strong
uv run python run_lab.py --score-only --pipelines p6
```

| | Pipeline | Idea |
|---|---|---|
| p1 | `p1_fixed_windows` | Equal windows, text only, no iteration. The control. |
| p2 | `p2_segment_escalate` | Segment, extract, escalate to pixels where unsure, reconcile. `regime_mode` switches between signal-derived and equal windows. |
| p3 | `p3_agentic_probe` | Give the model a day index and retrieval tools and let it decide what to look at. |
| p4 | `p4_anchor_induction` | Induce recurring screen archetypes from the archive, have a model name them, extract from the ones that announce state changes. |
| p5 | `p5_long_context` | The whole day's deduplicated text in one prompt. |
| p6 | `p6_recommended` | What the measurements point to: equal windows for coverage, salience-ranked anchors, bounded pixel escalation, then separate attribution and promotion passes. |

Model traffic goes through `lab/llm.py`, which caches every response by a hash of
the exact request. A re-run is therefore free and repeatable, and any prompt change
misses the cache. Usage and cost are recorded per run.

## Scoring

```bash
uv run python -m lab.evaluate out/runs/p6_recommended-latest.json
```

Recall, outcome correctness, trap violations and cost are reported separately. The
distinction matters: the ground truth is exhaustive for the four game matches but
not for every event in the day, so an unmatched prediction is judged as a plausible
event the ground truth does not list, as not an event at all, or as one of four
specific false claims the archive baits extractors into making.

## Layout

```
lab/spipe.py         read-only archive access; frame extraction by offset_index
lab/signals.py       per-frame change features, digests, deduplicated text
lab/visual.py        motion, stillness and perceptual hashes from the video chunks
lab/layout.py        typographic salience from per-word bounding boxes
lab/evidence.py      candidate ranking -- which frames are worth looking at
lab/schema.py        the event contract and the extraction rules all pipelines share
lab/groundtruth.py   hand-verified events and traps for 2026-07-24
lab/evaluate.py      scoring
bench_anchors.py     how well each candidate ranker surfaces decisive frames
verify_salience.py   salience coverage, precision@K, per-episode recall vs
                     text length, across four categories
site_visits.py       "how many times did I visit X" from three candidate
                     signals -- browser_url, a11y URLs, window titles
export_frames.py     frames to PNG + manifest, either on a systematic clock
                     grid or as the human-verified set
vlm_bench/           local vision-model benchmark (gemma4 E2B/E4B/12B)
  bench_gemma4.py    six prompts over a frame set, plus the follow-up loop
  score_vlm.py       deterministic scoring against per-frame expectations
  drive_all.sh       the sweep as run, one model per process
out/                 runs, scores, induced packs, caches (gitignored)
```

## The two frame sets, and why they are separate

`export_frames.py` produces either kind, and mixing them is how you get a
benchmark that looks good and means nothing.

A **systematic set** takes the frame nearest each point on a fixed clock interval.
It is what a collector at that capture frequency would actually send, and it is
chosen by the clock rather than by content, so it cannot be cherry-picked. Use it
to ask *would this cadence see the event at all*.

A **targeted set** is the frames a human verified, each carrying a note about what
a correct reading would say. Use it to ask *given the decisive frame, is it read
correctly*. `vlm_bench/score_vlm.py` holds those expectations as data, so scoring
is deterministic and no model judges another model.

```bash
uv run python export_frames.py --every 600 --out out/frames/grid600
uv run python export_frames.py --targeted --out out/frames/targeted
uv run python vlm_bench/score_vlm.py out/vlm/*.jsonl
```

The vision benchmark itself needs a GPU with the gemma4 weights cached; the sweep
as run is in `vlm_bench/drive_all.sh` and `drive_stage2.sh`. Note gemma-4-12B is
`model_type: gemma4_unified` and needs transformers ≥5.10.dev, while E2B and E4B
run on 5.7.

## Adding a ground-truth day

Pick a day, work through it with `lab.survey`, extract the decisive frames and look
at them, then add `TruthEvent` entries with the frame ids that prove each claim.
Add a trap entry for anything in the day that invites a specific false conclusion —
those are as valuable as the events, and they are what stop a pipeline from scoring
well by being confidently wrong.
