"""Spike: forced-align a handful of CoSHE clips (Devanagari+Latin Hinglish) with the
MMS-based ctc-forced-aligner to check timestamp quality before building the full
windowed dataset. Prints per-word timestamps + sanity stats."""

import glob
import io
import sys
import tempfile
import wave

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
import torch
from ctc_forced_aligner import (
    generate_emissions,
    get_alignments,
    get_spans,
    load_alignment_model,
    load_audio,
    postprocess_results,
    preprocess_text,
)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"device={device}", flush=True)

# grab N clips from first shard's first row group
f = sorted(glob.glob("/mnt/d/datasets/CoSHE-Eval/data/eval-*.parquet"))[0]
t = pq.ParquetFile(f).read_row_group(
    0, columns=["audio_file_name", "transcription", "audio"]
)
clips = []
for i in range(min(N, t.num_rows)):
    clips.append(
        (
            t["audio_file_name"][i].as_py(),
            t["transcription"][i].as_py(),
            t["audio"][i].as_py()["bytes"],
        )
    )

model, tokenizer = load_alignment_model(
    device, dtype=torch.float16 if device == "cuda" else torch.float32
)

for name, text, ab in clips:
    wav, sr = sf.read(io.BytesIO(ab))
    if wav.ndim > 1:
        wav = wav.mean(1)
    dur = len(wav) / sr
    # write 16k mono temp for load_audio
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        sf.write(tf.name, wav, sr)
        audio_waveform = load_audio(tf.name, model.dtype, model.device)
    emissions, stride = generate_emissions(model, audio_waveform, batch_size=1)
    tokens_starred, text_starred = preprocess_text(text, romanize=True, language="hin")
    segments, scores, blank = get_alignments(emissions, tokens_starred, tokenizer)
    spans = get_spans(tokens_starred, segments, blank)
    word_ts = postprocess_results(text_starred, spans, stride, scores)
    nwords_gt = len(text.split())
    monotonic = all(
        word_ts[i]["end"] >= word_ts[i]["start"] for i in range(len(word_ts))
    ) and all(
        word_ts[i + 1]["start"] >= word_ts[i]["start"] - 0.05
        for i in range(len(word_ts) - 1)
    )
    cov = word_ts[-1]["end"] if word_ts else 0
    print(
        f"\n=== {name}  dur={dur:.1f}s  gt_words={nwords_gt}  aligned={len(word_ts)}  "
        f"last_end={cov:.1f}s  coverage={cov/dur*100:.0f}%  monotonic={monotonic}",
        flush=True,
    )
    for w in word_ts[:6] + (["..."] if len(word_ts) > 12 else []) + word_ts[-6:]:
        if w == "...":
            print("    ...")
            continue
        print(f"    [{w['start']:6.2f}-{w['end']:6.2f}] {w['text']}")
