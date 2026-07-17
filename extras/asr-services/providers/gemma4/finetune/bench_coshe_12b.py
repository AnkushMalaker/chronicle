"""Benchmark base google/gemma-4-12B-it on CoSHE-Eval (Hinglish).

Faithful to the gemma4-asr service path that produced the E4B 26.23% number:
  * same diarization prompt
  * audio AFTER text, apply_chat_template + generate (model default sampling)
  * parse_response -> strip "Speaker N:" labels -> join
  * long clips (>30s) split into 30s windows, window texts concatenated

Selection matches mlexp.runners.dataset.run_coshe(limit=500, seed="coshe-eval"):
all audio_file_names in shard order, shuffled with random.Random(seed), first N.

Output JSONL (score_coshe schema): {audio_file_name, transcription, hyp, asr_seconds, duration_s}

  python bench_coshe_12b.py --data_dir /home/coshe-data/data --out /home/gemma4ft/out/coshe_12b.jsonl \
      --model google/gemma-4-12B-it --limit 500 --seed coshe-eval
"""

import argparse
import glob
import io
import json
import os
import random
import re
import time

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
import torch
from transformers import AutoModelForMultimodalLM, AutoProcessor

SR = 16000
# The gemma4-12B "unified" (encoder-free) model has NO hard 30s audio cap — its
# feature extractor encodes linearly at 25 tok/s with no truncation. CoSHE clips
# are all <=60s, so we feed each clip whole (window_seconds defaults large).
# Keep windowing logic for safety on hypothetical >window_seconds clips.

# Setup A — HF gemma-4-12B-it model-card ASR prompt (NOT the E4B diarization
# prompt). "in its original language" generalizes the card's {LANGUAGE}
# placeholder for CoSHE's code-switched Hindi/English. Plain transcription.
HF_ASR_PROMPT = (
    "Transcribe the following speech segment in its original language.\n\n"
    "Follow these specific instructions for formatting the answer:\n"
    "* Only output the transcription, with no newlines.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not "
    "one point seven, and write 3 instead of three."
)
# Setup B — strict verbatim transcription prompt (guards against the model
# translating Hinglish to English / adding commentary).
VERBATIM_PROMPT = (
    "You are an expert transcriptionist. Transcribe the provided Hinglish audio "
    "verbatim exactly as it is spoken. Output ONLY the transcribed text. Do not "
    "translate it to English, do not summarize, and do not add any conversational "
    "pleasantries or commentary."
)
PROMPTS = {"hf_asr": HF_ASR_PROMPT, "verbatim": VERBATIM_PROMPT}
SPEAKER_LINE_RE = re.compile(r"^(Speaker \d+):\s*(.+)$", re.MULTILINE)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-12B-it")
    p.add_argument("--data_dir", default="/home/coshe-data/data")
    p.add_argument("--out", default="/home/gemma4ft/out/coshe_12b.jsonl")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--seed", default="coshe-eval")
    # CoSHE transcripts are long/dense (mean ~882 chars); 512 truncates and
    # inflates WER (deletion trap). 1024 fits the ~60s clips without truncation.
    p.add_argument("--max_new_tokens", type=int, default=1024)
    p.add_argument(
        "--window_seconds",
        type=float,
        default=600.0,
        help="split clips longer than this; default large = whole-clip",
    )
    # HF card recommends temp=1.0/top_p=0.95/top_k=64 (general). Lower temp for ASR.
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--top_p", type=float, default=0.95)
    p.add_argument("--top_k", type=int, default=64)
    p.add_argument(
        "--greedy", action="store_true", help="do_sample=False (overrides temp)"
    )
    p.add_argument("--repetition_penalty", type=float, default=1.0)
    p.add_argument(
        "--no_repeat_ngram_size",
        type=int,
        default=0,
        help="block repeated n-grams (e.g. 3) to stop greedy loops",
    )
    p.add_argument(
        "--min_new_tokens",
        type=int,
        default=0,
        help="force >=N generated tokens (stops immediate-EOS empties)",
    )
    p.add_argument("--prompt", choices=list(PROMPTS), default="hf_asr")
    return p.parse_args()


def select_names(data_dir, limit, seed):
    """Replicate run_coshe deterministic selection."""
    shards = sorted(glob.glob(os.path.join(data_dir, "eval-*.parquet")))
    names = []
    for s in shards:
        names.extend(
            pq.read_table(s, columns=["audio_file_name"]).column(0).to_pylist()
        )
    if limit and limit < len(names):
        random.Random(seed).shuffle(names)
        return set(names[:limit]), shards
    return set(names), shards


def load_done(out_path):
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if "error" in rec:
                    continue
                if rec.get("audio_file_name"):
                    done.add(rec["audio_file_name"])
    return done


def decode_audio(raw):
    audio, sr = sf.read(io.BytesIO(raw), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=SR)
    return np.ascontiguousarray(audio, dtype=np.float32)


def windows(audio, window_seconds):
    """Whole clip if it fits; else non-overlapping windows of window_seconds."""
    w = int(window_seconds * SR)
    if len(audio) <= w:
        return [audio]
    return [audio[i : i + w] for i in range(0, len(audio), w)]


class Model:
    def __init__(
        self,
        model_id,
        max_new_tokens,
        window_seconds=600.0,
        prompt=HF_ASR_PROMPT,
        temperature=1.0,
        top_p=0.95,
        top_k=64,
        greedy=False,
        repetition_penalty=1.0,
        no_repeat_ngram_size=0,
        min_new_tokens=0,
    ):
        self.max_new_tokens = max_new_tokens
        self.window_seconds = window_seconds
        self.prompt = prompt
        self.do_sample = not greedy
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.repetition_penalty = repetition_penalty
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.min_new_tokens = min_new_tokens
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            model_id, dtype=torch.bfloat16, device_map="auto"
        )
        self.model.eval()

    def _decode(self, outputs, input_len):
        # Decode WITH special tokens so the channel markers survive, then strip
        # the thinking channel exactly like the chat template's strip_thinking
        # macro: drop everything inside <|channel>thought ... <channel|> blocks.
        raw = self.processor.decode(outputs[0][input_len:], skip_special_tokens=False)
        parts = []
        for part in raw.split("<channel|>"):
            parts.append(part.split("<|channel>")[0] if "<|channel>" in part else part)
        text = "".join(parts)
        # remove any residual angle-bracket control tokens (<|...|>, <...|>, <eos>, etc.)
        text = re.sub(r"<\|?[a-z_]+\|?>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def transcribe_window(self, audio):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": self.prompt},
                    {"type": "audio", "audio": audio},
                ],
            }
        ]
        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            add_generation_prompt=True,
            enable_thinking=False,
        ).to(self.model.device)
        input_len = inputs["input_ids"].shape[-1]
        gen_kwargs = {
            "max_new_tokens": self.max_new_tokens,
            "repetition_penalty": self.repetition_penalty,
        }
        if self.no_repeat_ngram_size:
            gen_kwargs["no_repeat_ngram_size"] = self.no_repeat_ngram_size
        if self.min_new_tokens:
            # the 12B-it often bails to immediate EOS (empty) on long clips;
            # forcing a floor unlocks the transcription it was withholding.
            gen_kwargs["min_new_tokens"] = self.min_new_tokens
        if self.do_sample:
            gen_kwargs.update(
                do_sample=True,
                temperature=self.temperature,
                top_p=self.top_p,
                top_k=self.top_k,
            )
        else:
            gen_kwargs.update(do_sample=False)  # greedy / deterministic
        with torch.inference_mode():
            outputs = self.model.generate(**inputs, **gen_kwargs)
        raw_text = self._decode(outputs, input_len)
        matches = list(SPEAKER_LINE_RE.finditer(raw_text))
        clean = (
            " ".join(m.group(2).strip() for m in matches if m.group(2).strip())
            if matches
            else raw_text.strip()
        )
        if clean == "[NO SPEECH]":
            clean = ""
        return clean

    def transcribe(self, audio):
        parts = [self.transcribe_window(w) for w in windows(audio, self.window_seconds)]
        return " ".join(p for p in parts if p).strip()


def main():
    args = parse_args()
    selected, shards = select_names(args.data_dir, args.limit, args.seed)
    done = load_done(args.out)
    target = selected - done
    print(
        f"selected={len(selected)} done={len(selected & done)} todo={len(target)}",
        flush=True,
    )
    if not target:
        print("nothing to do", flush=True)
        return

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    m = Model(
        args.model,
        args.max_new_tokens,
        args.window_seconds,
        prompt=PROMPTS[args.prompt],
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        greedy=args.greedy,
        repetition_penalty=args.repetition_penalty,
        no_repeat_ngram_size=args.no_repeat_ngram_size,
        min_new_tokens=args.min_new_tokens,
    )
    print(
        f"model loaded: {args.model} (prompt={args.prompt} thinking=False "
        f"do_sample={not args.greedy} temp={args.temperature} "
        f"top_p={args.top_p} top_k={args.top_k} rep_pen={args.repetition_penalty} "
        f"no_repeat_ngram={args.no_repeat_ngram_size} min_new={args.min_new_tokens} "
        f"max_new={args.max_new_tokens} window={args.window_seconds})",
        flush=True,
    )

    n_ok = n_err = 0
    t0 = time.time()
    with open(args.out, "a") as out_f:
        for shard in shards:
            if not target:
                break
            pf = pq.ParquetFile(shard)
            for batch in pf.iter_batches(batch_size=8):
                for row in batch.to_pylist():
                    name = row["audio_file_name"]
                    if name not in target:
                        continue
                    target.discard(name)
                    try:
                        audio = decode_audio(row["audio"]["bytes"])
                        dur = len(audio) / SR
                        ts = time.time()
                        hyp = m.transcribe(audio)
                        dt = time.time() - ts
                        rec = {
                            "audio_file_name": name,
                            "transcription": row["transcription"],
                            "hyp": hyp,
                            "asr_seconds": round(dt, 2),
                            "duration_s": round(dur, 2),
                        }
                        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out_f.flush()
                        n_ok += 1
                        done_n = n_ok + n_err
                        print(
                            f"  [{done_n}/{len(selected)}] {name} dur={dur:.0f}s "
                            f"gen={dt:.1f}s hyp[:80]={hyp[:80]!r}",
                            flush=True,
                        )
                    except Exception as e:
                        n_err += 1
                        out_f.write(
                            json.dumps(
                                {"audio_file_name": name, "error": str(e)},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        out_f.flush()
                        print(f"  ERR {name}: {e}", flush=True)
                    if not target:
                        break
    print(
        f"DONE ok={n_ok} err={n_err} elapsed={time.time()-t0:.0f}s out={args.out}",
        flush=True,
    )


if __name__ == "__main__":
    main()
