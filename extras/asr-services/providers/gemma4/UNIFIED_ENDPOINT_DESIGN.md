# Gemma 4 unified multimodal endpoint — design

## Problem

Gemma 4 E2B is **one combined multimodal model**: audio understanding and text generation
are the *same* forward pass over interleaved text+audio tokens. But the service exposes it
as a set of task-specific wrappers, and none of them accept **more than one audio clip per
request**:

| endpoint | audio in | limitation |
|----------|----------|-----------|
| `/transcribe` | 1 file (multipart) | single audio; ASR-only framing |
| `/v1/chat/completions` | none (text-only) | `ChatMessage.content: str`; no audio at all |
| `/judge` | 1 file | single audio + transcript |
| `/stream` (ws) | PCM frames | streaming ASR only |

Consequences:
1. **Multi-audio prompting is impossible.** Few-shot wake-word classification (show K example
   clips + a candidate in one prompt) can't be expressed → today it requires loading a *second*
   copy of the model in a one-off container, which OOMs the 24 GB GPU, so we **stop the live ASR
   service, run, and restart** every time. Redundant model load + ASR downtime + GPU contention.
2. **Redundant encode/decode round-trips.** The OpenAI `input_audio` transport is base64. For a
   *local* caller that already has a wav on disk, the path is `file → base64 → (wire) → base64-decode
   → temp file → processor re-reads+decodes`. Two of those steps are pure overhead.

## Core insight

Everything the model does is one operation:

```
generate(messages) -> text      # messages = interleaved [text | audio] parts
```

Transcribe, classify, judge, and chat are all **presets** of this. The clean shape is:

- a single **canonical multimodal generate** path on the loaded model,
- the **OpenAI-compatible chat/completions** endpoint as the *general* interface (this is the
  industry-standard way to serve a combined model — same schema vLLM/Gemini/OpenRouter use for
  audio: `content: [{type:"text"}, {type:"input_audio", input_audio:{data,format}}]`),
- the ASR-specific wrappers kept thin on top.

"Use the combined model as a general endpoint for both chat and ASR" = make `/v1/chat/completions`
multimodal; transcription becomes "chat with a transcribe prompt + one audio".

## Design — three layers

```
HTTP adapters (thin; format + presets)
  POST /v1/chat/completions   GENERAL multimodal chat (text+audio messages)   ← chat, classify, judge, 1-shot ASR
  POST /transcribe            ASR preset: silence-gate + >30s window/stitch + diarization-parse + NDJSON
  POST /classify   (optional) convenience: example clips+labels+candidate -> label
  POST /judge, WS /stream     existing presets
        │   normalize_audio()  ── decode EVERY transport to np.float32@16k mono, ONCE
        ▼
Transcriber.generate(messages_with_array_audio) -> (text, in_tok, out_tok)   ← single canonical path, loaded model
```

### Layer 1 — canonical generate (transcriber.py)
`generate(messages, *, max_tokens, temperature=0, top_p, top_k) -> (text, in_tok, out_tok)`
- `messages`: `[{role, content:[parts]}]`; parts are `{"type":"text",...}` or
  `{"type":"audio","audio": <np.ndarray>}` (mono float32 @ 16 kHz).
- The ONLY place that calls `apply_chat_template` + `model.generate` + `_decode_response`.
- No silence gate, no batching, no temp files — pure model access on the in-memory weights.
- `generate_chat()` (today text-only) becomes a thin caller → full back-compat for text chat.

### Layer 2 — audio normalization at the boundary (the round-trip fix)
One helper `normalize_audio_parts(content) -> content` that accepts audio in any transport and
decodes **exactly once to an array** (`soundfile.read(BytesIO)` / `load_audio_file`), never to disk:

| transport | when | cost |
|-----------|------|------|
| `input_audio` base64 | remote / cloud-parity | b64→BytesIO→array (no temp file) |
| multipart raw bytes | local, avoid +33% base64 tax | bytes→array |
| `audio_path` ref (shared RO volume) | trusted local batch | `load_audio_file(path)` → array, **zero re-encode** |

All converge to `{"type":"audio","audio": <array>}`. The `file→b64→file→decode` chain collapses to
a single decode regardless of caller. (Validation note: confirm the installed processor accepts an
ndarray under the `audio` key; today `_transcribe_single` passes a path. If a given transformers
version wants a path/URL, normalize to the processor's accepted form once — still one decode, in a
single place.)

### Layer 3 — HTTP endpoints (thin adapters)
- **`/v1/chat/completions`** — widen `ChatMessage.content` from `str` to `str | list[part]`; parts may
  include `input_audio`. Pass through `normalize_audio_parts` → `generate`. This is the general
  endpoint; classification/judge/one-shot-ASR are just prompts over it.
- **`/transcribe`** — unchanged contract; still owns the ASR-only concerns the general endpoint
  shouldn't carry (silence gate, >30 s windowing+stitch, diarization parse, NDJSON progress).
  Internally routed through `normalize_audio_parts` + `generate`.
- **`/classify`** (optional sugar) — multipart `examples[]` + `labels[]` + `candidate` → builds the
  few-shot messages server-side → returns `{label, raw}`. Saves callers from hand-building messages.
- `/judge`, `/stream` — refactor to call `generate`; behavior unchanged.

## Consistency: OpenAI ↔ Gemma

- The OpenAI `input_audio` part maps **deterministically** to Gemma's `{"type":"audio"}` part — a
  faithful 1:1 adapter, *if normalized at the boundary*. Same payload works against OpenRouter/Gemini
  **and** the local service → identical client code, just swap `base_url`. (This is what lets
  `threeway_probe.py` target cloud or local unchanged.)
- One Gemma-specific nuance: it prefers the audio **after** its text label in a turn. We preserve
  client-provided part order; presets emit the correct order. So consistency holds.
- The chat path deliberately **skips the silence gate** (that's an ASR-output concern) — correct for
  classification, where we want a verdict even on near-silent clips.

## Transport guidance (answering "just random round trips")

- **Remote/cloud:** base64 `input_audio` is unavoidable (no shared FS) — but we decode once to an
  array, never to a temp file.
- **Local batch (triage over pending clips):** prefer **`audio_path` refs** with the wakeword
  `data/samples` volume mounted read-only into the asr container → the service reads each wav
  straight to an array. No base64, no temp files, no copies. Multipart raw is the middle option.

## Impact / migration
- **Back-compat:** `/transcribe` and text-only `/v1/chat` behave exactly as today. `ChatMessage.content`
  widened to `str | list` (additive). One image rebuild.
- **Deletes the stop/restart workflow:** multi-audio few-shot classification is served by the *live*
  model — no second load, no ASR downtime, no extra idle VRAM (same weights).
- **Less code, one behavior:** transcribe/classify/judge/chat unified on one generate path.
- **Concurrency:** GPU generate is serial; a batch labeling job shares the card with live ASR/wake
  transcription. Fine for background triage; if it matters, route batch on the existing ASR "normal"
  lane (priority lane keeps wake clips ahead). See `asr-priority-lane`.

## Status — IMPLEMENTED + validated (2026-06-14)

Done (minimal, backward-compatible):
- `common/audio_utils.py`: added `load_audio_bytes(bytes)` — in-memory WAV→array (no temp file),
  reuses `convert_audio_to_numpy`/`convert_to_mono`/`resample_audio`.
- `providers/gemma4/transcriber.py`: added `_normalize_chat_content()` and wired it into
  `generate_chat()`. `input_audio` base64 → decode once to ndarray; **path refs + ndarrays + text
  pass straight through** (processor loads paths natively). No new endpoint, no schema change —
  `/v1/chat/completions` already forwards `messages: list[dict]`, so audio parts just flow through.
- Verified the processor accepts an **ndarray** under `audio` (not only a path), so no temp files.

Validated on the LIVE service (no stop/restart, no second model load):
- Multi-audio few-shot through `/v1/chat/completions` works on the running model.
- 3-way on the 27 held-out clips via the endpoint = **96.3% (26/27)** vs **100% in-container**.
  The single flip is a borderline `hermes`: in-container passed **file paths** (transformers' own
  loader), the endpoint sent **base64** decoded by our `wave` loader (`/32767` scaling) — slightly
  different decode flips one boundary case (both greedy).

Decode-path takeaway: for **local** callers, prefer **path refs** (mount `data/samples` RO into the
asr container, send `{"type":"audio","audio":"<container path>"}`) → exact `/transcribe` parity, no
base64, no decode. base64 stays for remote/cloud where there's no shared FS (accept the 1-in-27
boundary wobble, or align the loaders for bit-parity later).

NOTE: the running image was built before the final "path refs pass through" tweak; rebuild to
activate path-ref parity. base64 flow is already live and correct.

## Conclusion (where we stopped, 2026-06-14)

**Validated + serving primitive built; the wakeword-service integration is deferred.**
- Approach proven: 3-way few-shot ("hey hermes" / "hermes" / "nothing"), gemma4 E2B best
  (98% labeled, 100%/96% on the user's 27 held-out hand-labels), local + free.
- `/v1/chat/completions` now serves multi-audio on the live model — no stop/restart.
- NOT built: any loop inside `extras/wakeword-service/` (no trigger, no pending triage, no
  suggestion write/surface). Probe scripts under `ml-experiments/.../wakeword_gemma_classify/`
  are eval harnesses only.

**Eventual shape (per user):** mostly a **button** in the review UI ("suggest labels") that calls the
classifier over `pending/` and pre-fills suggestions for confirm/correct — NOT an autonomous labeler.

**Key open problem before that ships: a reliable "unsure"/abstain.** The classifier currently makes a
forced 3-way choice; for a triage button it must *defer* hard cases to the human instead of guessing.
Candidate signals (none built yet):
- explicit 4th label "unsure" in the prompt (cheap, but models rarely self-abstain reliably);
- self-consistency — run N times with shuffled few-shot order / different ref sets, abstain on
  disagreement (needs sampling or perturbation since greedy is deterministic);
- two-stage classify→verify, abstain when they disagree;
- token-logprob confidence if we expose it from `generate` (top-token prob as a calibrated score);
- cheap energy/VAD pre-gate to auto-route near-silence to "nothing" (note: a real wake existed at
  rms 0.0023, so a global floor is unsafe — use only as a soft signal).
The abstain mechanism, not the classifier, is the remaining design work.

## Build order
1. `transcriber.generate()` canonical path + refactor `generate_chat` to delegate.
2. `normalize_audio_parts()` (base64 + multipart + path), decode-once-to-array.
3. Widen `/v1/chat/completions` schema + wire normalization.
4. (optional) `/classify` convenience preset.
5. Rebuild image. Point `threeway_probe.py` / a new `classify_pending.py` at `localhost:8767` — zero restarts.
