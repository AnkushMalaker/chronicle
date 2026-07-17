"""Fast 12B ASR config sweep on a fixed CoSHE subset, via the REAL service path.

Loads google/gemma-4-12B-it ONCE (Gemma4Transcriber, the production transcriber)
and runs a small fixed set of CoSHE-Eval clips through several configs, varying
prompt / windowing / decode strategy. Output is one JSONL per config in the
score_coshe schema so evaluate/score_coshe.py can score them directly.

Unlike bench_coshe_12b.py (manual channel-strip decode, whole-clip 600s window)
this uses the transcriber's parse_response decode + 30s windowing + stitching —
i.e. exactly what the deployed gemma4-asr service does — so a winning config maps
straight to env knobs (GEMMA4_QUANT, TRANSCRIPTION_PROMPT, MAX_NEW_TOKENS,
BATCH_DURATION_SECONDS, BATCH_THRESHOLD_SECONDS).

Run inside chronicle-asr-gemma4-smoke (transformers 5.10.1 + bnb), GPU, with
/models = HF cache, /coshe-data = CoSHE parquet, /data/coshe-eval mounted.
"""

import argparse
import glob
import io
import json
import os
import tempfile
import time
import wave

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf

SR = 16000

# 11 fixed samples chosen from the existing 500-sample run to span the
# distribution: catastrophic-tail (loops/empties), median, and easy clips.
SAMPLES = [
    "audio_1400.wav",
    "audio_299.wav",
    "audio_353.wav",
    "audio_131.wav",
    "audio_564.wav",
    "audio_1404.wav",
    "audio_243.wav",
    "audio_666.wav",
    "audio_1543.wav",
    "audio_650.wav",
    "audio_1938.wav",
]

# Prompts under test ----------------------------------------------------------
PROD_PROMPT = (  # current service default (DEFAULT_TRANSCRIPTION_PROMPT)
    "Transcribe the following speech segment in its original language into text. "
    "Only output the transcription text itself, with no commentary or explanation. "
    "When transcribing numbers, write the digits, i.e. write 1.7 and not "
    "one point seven, and write 3 instead of three."
)
HF_PROMPT = (
    "Transcribe the following speech segment in its original language.\n\n"
    "Follow these specific instructions for formatting the answer:\n"
    "* Only output the transcription, with no newlines.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not "
    "one point seven, and write 3 instead of three."
)
VERBATIM_PROMPT = (
    "You are an expert transcriptionist. Transcribe the provided Hinglish audio "
    "verbatim exactly as it is spoken. Output ONLY the transcribed text. Do not "
    "translate it to English, do not summarize, and do not add any conversational "
    "pleasantries or commentary."
)
HINGLISH_PROMPT = (  # explicit code-switch steer
    "Transcribe the following Hindi-English code-mixed (Hinglish) speech verbatim. "
    "Write each word in the language it is spoken. Only output the transcription "
    "text itself, with no commentary. When transcribing numbers, write the digits."
)
# prod prompt WITHOUT the "write digits" clause (CoSHE refs spell numbers out, so
# the digit instruction inflates WER) and without code-switch wording.
NODIGIT_PROMPT = (
    "Transcribe the following speech segment in its original language into text. "
    "Only output the transcription text itself, with no commentary or explanation."
)

# Each config: (prompt, threshold_s, window_s, overlap_s, max_new, extra_gen) ---
WHOLE = 100000.0
CONFIGS = {
    # production defaults: 30s windows + overlap, greedy, 512 tokens
    "prod": (PROD_PROMPT, 30.0, 30.0, 5.0, 512, {}),
    # isolate windowing: production prompt but whole-clip (matches old bench geometry)
    "prod_whole": (PROD_PROMPT, WHOLE, 30.0, 5.0, 1024, {}),
    # anti-loop safety on the windowed path
    "prod_norep3": (PROD_PROMPT, 30.0, 30.0, 5.0, 512, {"no_repeat_ngram_size": 3}),
    # prompt variants on the windowed path
    "hfprompt": (HF_PROMPT, 30.0, 30.0, 5.0, 512, {}),
    "verbatim": (VERBATIM_PROMPT, 30.0, 30.0, 5.0, 512, {}),
    "hinglish": (HINGLISH_PROMPT, 30.0, 30.0, 5.0, 512, {}),
    # low-temp sampling on the windowed path
    "sample_t03": (
        PROD_PROMPT,
        30.0,
        30.0,
        5.0,
        512,
        {"do_sample": True, "temperature": 0.3, "top_p": 0.95, "top_k": 64},
    ),
    # round 2: best prompts + anti-loop n-gram (tail-safe), windowed greedy
    "hf_norep3": (HF_PROMPT, 30.0, 30.0, 5.0, 512, {"no_repeat_ngram_size": 3}),
    "hinglish_norep3": (
        HINGLISH_PROMPT,
        30.0,
        30.0,
        5.0,
        512,
        {"no_repeat_ngram_size": 3},
    ),
    "nodigit_norep3": (
        NODIGIT_PROMPT,
        30.0,
        30.0,
        5.0,
        512,
        {"no_repeat_ngram_size": 3},
    ),
}


def select_rows(data_dir, names):
    """Pull the target rows from the parquet shards."""
    want = set(names)
    found = {}
    for s in sorted(glob.glob(os.path.join(data_dir, "eval-*.parquet"))):
        pf = pq.ParquetFile(s)
        for batch in pf.iter_batches(batch_size=64):
            for row in batch.to_pylist():
                if row["audio_file_name"] in want:
                    found[row["audio_file_name"]] = row
        if len(found) == len(want):
            break
    return found


def write_wav(raw_bytes, path):
    audio, sr = sf.read(io.BytesIO(raw_bytes), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=SR)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm16.tobytes())
    return len(audio) / SR


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_dir", default="/coshe-data")
    ap.add_argument("--out_dir", default="/data/coshe-eval/results/12b_sweep")
    ap.add_argument("--model", default="google/gemma-4-12B-it")
    ap.add_argument("--quant", default="4bit")
    ap.add_argument("--configs", default="", help="comma list; default = all")
    args = ap.parse_args()

    # Force the transcriber into the desired load mode BEFORE import/instantiation.
    os.environ["GEMMA4_QUANT"] = args.quant
    os.environ["GEMMA4_MTP"] = "0"  # off: clean target distribution, faster load
    os.environ["ASR_MODEL"] = args.model
    os.environ["TORCH_DTYPE"] = "bfloat16"

    from providers.gemma4.transcriber import Gemma4Transcriber

    os.makedirs(args.out_dir, exist_ok=True)

    print(f"selecting {len(SAMPLES)} samples from {args.data_dir} ...", flush=True)
    rows = select_rows(args.data_dir, SAMPLES)
    missing = set(SAMPLES) - set(rows)
    if missing:
        print(f"WARNING missing samples: {missing}", flush=True)

    # Decode all target clips to temp WAVs once.
    tmp = tempfile.mkdtemp(prefix="coshe_")
    clips = {}  # name -> (wav_path, ref_text, duration_s)
    for name in SAMPLES:
        if name not in rows:
            continue
        wav_path = os.path.join(tmp, name)
        dur = write_wav(rows[name]["audio"]["bytes"], wav_path)
        clips[name] = (wav_path, rows[name]["transcription"], dur)
    print(f"decoded {len(clips)} clips", flush=True)

    # Load model once.
    t = Gemma4Transcriber(model_id=args.model)
    t0 = time.time()
    t.load_model()
    print(f"model loaded quant={args.quant} in {time.time()-t0:.0f}s", flush=True)

    # Monkeypatch _generate to merge per-config extra gen kwargs (anti-loop/sampling).
    orig_generate = t._generate
    t._extra_gen = {}

    def patched_generate(inputs, **kw):
        kw.update(t._extra_gen)
        return orig_generate(inputs, **kw)

    t._generate = patched_generate

    selected = args.configs.split(",") if args.configs else list(CONFIGS)
    for cfg_name in selected:
        prompt, thr, win, ov, max_new, extra = CONFIGS[cfg_name]
        t.batch_threshold = thr
        t.batch_duration = win
        t.batch_overlap = ov
        t.max_new_tokens = max_new
        t._extra_gen = dict(extra)
        out_path = os.path.join(args.out_dir, f"{cfg_name}.jsonl")
        print(
            f"\n=== CONFIG {cfg_name} (thr={thr} win={win} max_new={max_new} "
            f"extra={extra}) -> {out_path}",
            flush=True,
        )
        with open(out_path, "w") as f:
            for name in SAMPLES:
                if name not in clips:
                    continue
                wav_path, ref, dur = clips[name]
                ts = time.time()
                try:
                    res = t.transcribe(wav_path, prompt_override=prompt)
                    hyp = res.text
                except Exception as e:
                    hyp = ""
                    print(f"  ERR {name}: {e}", flush=True)
                dt = time.time() - ts
                rec = {
                    "audio_file_name": name,
                    "transcription": ref,
                    "hyp": hyp,
                    "asr_seconds": round(dt, 2),
                    "duration_s": round(dur, 2),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                print(
                    f"  [{name}] dur={dur:.0f}s gen={dt:.1f}s "
                    f"hyp[:70]={hyp[:70]!r}",
                    flush=True,
                )
    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
