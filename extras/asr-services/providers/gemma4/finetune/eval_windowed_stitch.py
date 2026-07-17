"""Evaluate a model (base or LoRA adapter) on the held-out CoSHE test clips via the
windowed-stitch path: cut each whole clip into its honest <=28s windows (the SAME
forced-alignment windows used for training, from windows_v2.jsonl), transcribe each window
with PLAIN_PROMPT, then concatenate window hyps in order -> a full-clip hypothesis scored
against the full GT transcript.

Non-overlapping windows -> stitch is a plain concat (no overlap dedup). The same windowing
+ prompt + decode settings are used for base and every adapter, so the base->FT WER delta
is controlled. use_cache=True is safe (gemma4 cache bug fixed in transformers >=5.10).

Out: mlexp-schema JSONL keyed by audio_file_name: {transcription (full GT), hyp (stitched),
n_wins, duration_s}. Resumable (skips clips already written).
"""

import argparse
import glob
import io
import json
import os
import random
import time
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
import torch
from data_windowed import PLAIN_PROMPT
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

# Exact prod/benchmark prompt (interleave_probe.py PLAIN_PROMPT) that yields E2B ~10.6%
# median on CoSHE-100. gemma4 is prompt-conditioned; an elaborate prompt degrades base.
MINIMAL_PROMPT = (
    "Transcribe the following speech in its original language into text. "
    "Only output the transcription text itself, with no commentary or explanation."
)
PROMPTS = {"plain": PLAIN_PROMPT, "minimal": MINIMAL_PROMPT}


def load_done(path):
    done = set()
    if os.path.exists(path):
        for line in open(path):
            try:
                done.add(json.loads(line)["audio_file_name"])
            except Exception:
                pass
    return done


@torch.inference_mode()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemma-4-E2B-it")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--windows", default="/home/coshe_windowed/windows_v2.jsonl")
    ap.add_argument("--split", default="/home/coshe_windowed/windowed_split.json")
    ap.add_argument("--parquet_glob", default="/home/coshe/data/eval-*.parquet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch_size", type=int, default=8)
    ap.add_argument("--max_new_tokens", type=int, default=256)
    ap.add_argument(
        "--no_repeat_ngram",
        type=int,
        default=0,
        help="generate no_repeat_ngram_size; 3 kills greedy repetition-collapse",
    )
    ap.add_argument("--repetition_penalty", type=float, default=1.0)
    ap.add_argument(
        "--limit", type=int, default=0, help="cap #test clips (seeded subset)"
    )
    ap.add_argument(
        "--quant",
        default="bf16",
        choices=["bf16", "4bit"],
        help="bf16 = prod config (no quant); 4bit only for tiny GPUs",
    )
    ap.add_argument(
        "--prompt",
        default="plain",
        choices=["plain", "minimal"],
        help="minimal = exact benchmark/prod prompt (E2B ~10.6%); plain = training prompt",
    )
    ap.add_argument(
        "--names_file",
        default="",
        help="JSON list of clip names to restrict eval to (∩ held-out)",
    )
    ap.add_argument(
        "--inject",
        default="twostep",
        choices=["twostep", "onestep"],
        help="onestep = prod path apply_chat_template(tokenize=True,return_dict=True)",
    )
    ap.add_argument(
        "--fixed_win",
        type=float,
        default=0.0,
        help="ignore fa windows; split each clip into fixed N-sec windows (benchmark style)",
    )
    ap.add_argument(
        "--attn",
        default="",
        help="attn_implementation; empty = model default (prod). 'eager' degrades gemma4 gen",
    )
    args = ap.parse_args()
    prompt_text = PROMPTS[args.prompt]

    test_clips = json.load(open(args.split))["test_clips"]
    if args.names_file:
        want = set(json.load(open(args.names_file)))
        test_clips = [c for c in test_clips if c in want]
    if args.limit and args.limit < len(test_clips):
        test_clips = sorted(random.Random(0).sample(test_clips, args.limit))
    test_set = set(test_clips)
    done = load_done(args.out)
    todo = [c for c in test_clips if c not in done]

    wins_by_clip = defaultdict(list)
    for line in open(args.windows):
        r = json.loads(line)
        if r["audio_file_name"] in test_set:
            wins_by_clip[r["audio_file_name"]].append(r)
    for c in wins_by_clip:
        wins_by_clip[c].sort(key=lambda w: w["win_idx"])

    # decode needed test clips from parquet (audio + full GT), slice window audio
    gt, clip_audio = {}, {}
    need = set(todo)
    for f in sorted(glob.glob(args.parquet_glob)):
        if not need:
            break
        t = pq.ParquetFile(f).read(
            columns=["audio_file_name", "transcription", "audio"]
        )
        names = t["audio_file_name"].to_pylist()
        for ci, name in enumerate(names):
            if name not in need:
                continue
            gt[name] = t["transcription"][ci].as_py().strip()
            wav, sr = sf.read(io.BytesIO(t["audio"][ci].as_py()["bytes"]))
            if wav.ndim > 1:
                wav = wav.mean(1)
            if sr != 16000:
                import librosa

                wav = librosa.resample(
                    wav.astype(np.float32), orig_sr=sr, target_sr=16000
                )
            clip_audio[name] = np.ascontiguousarray(wav, dtype=np.float32)
            need.discard(name)
    print(
        f"test clips total={len(test_clips)} todo={len(todo)} decoded={len(clip_audio)}",
        flush=True,
    )

    proc = AutoProcessor.from_pretrained(args.model)
    load_kw = dict(dtype=torch.bfloat16, device_map="auto")
    if (
        args.attn
    ):  # benchmark/prod uses the MODEL DEFAULT (sdpa); eager degrades gemma4 gen
        load_kw["attn_implementation"] = args.attn
    if args.quant == "4bit":  # QLoRA-training config; degrades small E2B at inference
        load_kw["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            llm_int8_skip_modules=[
                "model.audio_tower",
                "model.vision_tower",
                "model.embed_audio",
                "model.embed_vision",
                "lm_head",
            ],
        )
    model = AutoModelForMultimodalLM.from_pretrained(args.model, **load_kw)
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter)
        print(f"loaded adapter: {args.adapter}", flush=True)
    model.eval()
    model.config.use_cache = True
    proc.tokenizer.padding_side = "left"

    # optional: ignore fa windows, split each clip into fixed N-sec windows (benchmark style)
    if args.fixed_win:
        n = int(args.fixed_win * 16000)
        wins_by_clip = {
            c: [
                {
                    "win_idx": i,
                    "start": (i * n) / 16000,
                    "end": min(len(clip_audio[c]), (i + 1) * n) / 16000,
                }
                for i in range(max(1, (len(clip_audio[c]) + n - 1) // n))
            ]
            for c in todo
        }

    # flatten all windows of all todo clips into one work list, batch across clips
    work = []  # (clip, win_idx, audio_slice)
    for c in todo:
        for w in wins_by_clip[c]:
            a0, a1 = int(w["start"] * 16000), int(w["end"] * 16000)
            work.append((c, w["win_idx"], clip_audio[c][a0:a1]))
    print(f"windows to transcribe: {len(work)}", flush=True)

    gen_kw = dict(
        max_new_tokens=args.max_new_tokens,
        do_sample=False,
        use_cache=True,
        repetition_penalty=args.repetition_penalty,
    )
    if args.no_repeat_ngram:
        gen_kw["no_repeat_ngram_size"] = args.no_repeat_ngram

    hyp_parts = defaultdict(dict)  # clip -> {win_idx: hyp}
    fout = open(args.out, "a")
    t0 = time.time()
    bs = 1 if args.inject == "onestep" else args.batch_size
    for s in range(0, len(work), bs):
        batch = work[s : s + bs]
        if (
            args.inject == "onestep"
        ):  # prod path: tokenize=True one-shot, per-window (bs=1)
            import os as _os
            import tempfile
            import wave as _wave

            c, wi, au = batch[0]
            # match the benchmark exactly: write a 16k PCM_16 WAV and pass the PATH (not the
            # raw array) — the Gemma4 processor conditions differently on array vs file.
            tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            with _wave.open(tf.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes((np.clip(au, -1, 1) * 32767).astype(np.int16).tobytes())
            msgs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "audio", "audio": tf.name},
                    ],
                }
            ]
            inp = proc.apply_chat_template(
                msgs,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                add_generation_prompt=True,
                enable_thinking=False,
            ).to(model.device)
            out = model.generate(**inp, **gen_kw)
            _os.unlink(tf.name)
            hyp_parts[c][wi] = proc.decode(
                out[0][inp["input_ids"].shape[-1] :], skip_special_tokens=True
            ).strip()
        else:  # legacy two-step path
            texts, audios = [], []
            for _, _, au in batch:
                msgs = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {"type": "audio", "audio": au},
                        ],
                    }
                ]
                texts.append(
                    proc.apply_chat_template(
                        msgs,
                        tokenize=False,
                        add_generation_prompt=True,
                        enable_thinking=False,
                    )
                )
                audios.append(au)
            inp = proc(text=texts, audio=audios, return_tensors="pt", padding=True).to(
                model.device
            )
            out = model.generate(**inp, **gen_kw)
            for i, (c, wi, _) in enumerate(batch):
                hyp_parts[c][wi] = proc.decode(
                    out[i][inp["input_ids"].shape[-1] :], skip_special_tokens=True
                ).strip()
        if (s // bs) % 40 == 0:
            print(f"[{s + len(batch)}/{len(work)}] {time.time() - t0:.0f}s", flush=True)

    # stitch + write per fully-completed clip
    for c in todo:
        parts = hyp_parts.get(c, {})
        if len(parts) != len(wins_by_clip[c]):
            continue  # incomplete (shouldn't happen); skip to retain resumability
        stitched = " ".join(parts[i] for i in sorted(parts)).strip()
        dur = round(len(clip_audio[c]) / 16000.0, 2)
        fout.write(
            json.dumps(
                {
                    "audio_file_name": c,
                    "transcription": gt[c],
                    "hyp": stitched,
                    "n_wins": len(parts),
                    "duration_s": dur,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    fout.close()
    print(f"DONE out={args.out}", flush=True)


if __name__ == "__main__":
    main()
