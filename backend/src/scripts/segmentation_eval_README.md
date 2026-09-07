# Chronicle timeline segmentation — eval set

Replay Chronicle's **segmentation** step against another model without a Chronicle
deployment. Segmentation is the step that decides *what an episode is*: it reads one
local day of evidence and emits bounded episodes with a title, summary, kind, salience,
and grounded assertions.

Reference outputs here were produced by **Codex / gpt-5.6-luna**, prompt version
`timeline-episodes-v11`. They are a strong baseline, **not ground truth** — nobody
hand-labelled these days.

> **This is real personal data**: transcripts of the vault owner's conversations,
> meetings, and screen activity, with real names. Treat the archive accordingly, and
> note that uploading it to a hosted GPU provider puts it on someone else's disk.

## What the task is

Given a day's evidence, produce `episodes.json` matching `output_schema.json`.

An episode is a **semantic** span, not a capture artifact. This is the whole point:
audio is recorded in bounded compute windows (30 min, or 2 h with a meeting signal), so
a single standup can be split across two recordings and an eight-hour idle stretch can
be one. The agent must cut on what was happening, not on where the recorder stopped.

## Layout

```
prompt.txt                     the task prompt, verbatim
output_schema.json             strict JSON schema the output must satisfy
index.json                     per-day stats + totals
days/<YYYY-MM-DD>/
    workspace/                 EXACTLY what the agent is given
        README.md              in-workspace instructions
        windows/index.json     coverage windows (20 min, 3 min overlap)
        windows/0000.json …    evidence sliced per window
        work/                  scratch space the agent may use
    evidence.json              the full manifest, for analysis only
    episodes.json              reference output (Codex/gpt-5.6-luna)
```

**Use `workspace/`, not `evidence.json`.** The agent never sees the flat evidence list —
it reaches 7.3 MB on a busy day. It gets windows and is told to walk them in order.
Feeding a model `evidence.json` is a different (easier, and much longer-context) task,
so results would not be comparable to the reference.

Windows guarantee coverage. **They are not episode boundaries** — an episode may span
several windows or sit inside one.

## Corpus

23 settled local days, 1,504 evidence items, 4.89 M transcript characters, 118 reference
episodes.

Days are wildly uneven and that is the interesting part:

| day | evidence items | transcript chars | episodes |
|---|---:|---:|---:|
| 2026-06-30 | 1 | 983 | 1 |
| 2026-06-13 | 9 | 34,151 | 8 |
| 2026-06-17 | 12 | 112,625 | 12 |
| 2026-06-14 | 7 | 121,476 | 6 |
| **2026-08-08** | **1,392** | **3,978,945** | **20** |

`2026-08-08` is 81 % of the corpus by text, and it is **not the same task** as the other
days. Its 1,392 items break down as:

```
observation    1,127    3,805,950 chars   96% of the day's text
audio_span       207            0
capture_gap       41            0
transcript        15      172,951         4.3%
frame              2           44
```

An `observation` is a ScreenPipe application/window context — app name, window title,
browser URL, and OCR/accessibility text scraped off the screen. The text is largely
browser chrome and menu entries (`Save Image As…`, `Copy Image Link`, tab titles), not
speech. Median excerpt 2,346 chars; p90 *and* max are both 6,000, so a large share are
truncated at the cap.

So this day measures **segmenting screen activity**, while the other 22 mostly measure
**segmenting conversation**. Bucket it separately rather than averaging it in — a model
tuned for one can look terrible at the other for reasons that have nothing to do with
boundary quality. It is still the useful stress case (a model that emits 200 episodes or
1 has failed in opposite directions, and both read as plausible from titles alone).

Evidence kinds across the corpus, for filtering: `transcript` (speech, with speaker
attribution), `observation` (screen/application context), `audio_span` (an audio interval
with no text), `capture_gap` (recorder not running), `frame` (a screenshot anchor),
`manual_memory` (deliberately saved by the user).

## What to measure

Boundary quality is the thing worth optimising, and it is not captured by text overlap.

1. **Episode count** per day vs reference. Over-segmentation (an episode per window) and
   under-segmentation (one episode per day) are the two failure modes, and both read as
   "plausible" if you only look at titles.
2. **Boundary agreement** — for each reference episode, the best temporal IoU against any
   predicted episode. Report mean IoU and the fraction matched above 0.5.
3. **Grounding** — every assertion carries `evidence_ids`. An assertion citing evidence
   outside its own episode's time bounds is a hallucinated span. This is cheap to check
   and catches the failure that reads best.
4. **`conversational` accuracy.** This flag matters in Chronicle: it promotes a
   capture-evidence recording back into the user-facing Recordings list. A false positive
   surfaces ambient room audio as a meeting.
5. **Salience distribution.** A model that marks everything `notable` has not made a
   judgement.

Title/summary similarity is worth reporting but is the weakest signal — two correct
summaries of the same span can share few tokens.

## Replaying

Point a model at one day's `workspace/`, give it `prompt.txt`, require
`output_schema.json`, and diff its `episodes.json` against the reference. The prompt
assumes filesystem access (read `windows/*.json`, write `episodes.json`); with an API
model, concatenating the window files in order and requiring structured output is the
closest equivalent — note that this changes the task from agentic file-walking to
single-shot, which is itself a variable worth isolating.

Chronicle's own executor is `services/timeline/codex_executor.py`; the workspace layout
is `services/timeline/workspace.py` and the prompt is `services/timeline/prompt.py`.

## Regenerating

```bash
podman exec -e MONGODB_SOCKET_TIMEOUT_MS=1800000 backend_chronicle-backend_1 \
  python /app/src/scripts/export_segmentation_dataset.py \
  --user-id <user_id> --output /app/data/backups/segmentation-eval
```

Exports every day whose `memory_state` is `written`.
