"""FREE local forced alignment of CoSHE audio to its ground-truth transcript, on the
4090, via ctc-forced-aligner (MMS uroman CTC). Times the GROUND-TRUTH words directly —
no hyp<->GT reconciliation.

IMPORTANT: the high-level get_word_stamps() does NOT romanize, so it silently drops
Devanagari (keeps only Latin words). We use the lower-level ONNX pipeline with
preprocess_text(romanize=True) so Hinglish (Devanagari + Latin) aligns fully. Needs
onnxruntime-gpu for CUDA.

Emits the normalized JSONL segment_by_alignment.py consumes (words = GT words + times):
    {"audio_file_name": "...", "words": [{"word": "<gt>", "start": 1.2, "end": 1.4}, ...]}

Usage:
    python fa_align.py --manifest overfit.json --out fa_words.jsonl --language hin
"""

import argparse
import json
import os
import time
from pathlib import Path

import onnxruntime
from ctc_forced_aligner import (
    MODEL_URL,
    Tokenizer,
    ensure_onnx_model,
    generate_emissions,
    get_alignments,
    get_spans,
    load_audio,
    postprocess_results,
    preprocess_text,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--language",
        default="hin",
        help="uroman iso (hin romanizes Devanagari; Latin passes through)",
    )
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument(
        "--model-path", default=os.path.expanduser("~/.cache/ctc_fa/mms_fa.onnx")
    )
    args = ap.parse_args()

    Path(args.model_path).parent.mkdir(parents=True, exist_ok=True)
    ensure_onnx_model(args.model_path, MODEL_URL)
    providers = onnxruntime.get_available_providers()
    use = (
        ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if "CUDAExecutionProvider" in providers
        else ["CPUExecutionProvider"]
    )
    print(f"onnx providers: {providers} -> using {use}", flush=True)
    session = onnxruntime.InferenceSession(args.model_path, providers=use)
    tokenizer = Tokenizer()

    rows = [json.loads(l) for l in open(args.manifest)]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    with open(out_path, "w") as out_f:
        for r in rows:
            audio = load_audio(r["audio_filepath"], ret_type="np")
            emissions, stride = generate_emissions(
                session, audio, batch_size=args.batch_size
            )
            tokens_starred, text_starred = preprocess_text(
                r["text"],
                romanize=True,
                language=args.language,
            )
            segments, scores, blank = get_alignments(
                emissions, tokens_starred, tokenizer
            )
            spans = get_spans(tokens_starred, segments, blank)
            word_ts = postprocess_results(text_starred, spans, stride, scores)
            words = [
                {
                    "word": w["text"],
                    "start": round(w["start"], 3),
                    "end": round(w["end"], 3),
                }
                for w in word_ts
            ]
            out_f.write(
                json.dumps(
                    {
                        "audio_file_name": r["audio_file_name"],
                        "words": words,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out_f.flush()
            gt_n = len(r["text"].split())
            print(
                f"  {r['audio_file_name']}: {len(words)} aligned / {gt_n} GT words "
                f"[{words[0]['start'] if words else 0:.1f}..{words[-1]['end'] if words else 0:.1f}s]",
                flush=True,
            )
    print(f"DONE {len(rows)} clips in {time.time()-t0:.0f}s -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
