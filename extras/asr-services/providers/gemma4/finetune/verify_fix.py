"""Verify enable_thinking=False + no_repeat_ngram fixes the bad clips."""

import glob
import os
import sys

sys.path.insert(0, "/home/gemma4ft")
import pyarrow.parquet as pq
from bench_coshe_12b import SR, VERBATIM_PROMPT, Model, decode_audio

DATA = "/home/coshe-data/data"
TARGETS = [
    "audio_1400.wav",
    "audio_299.wav",
    "audio_160.wav",
    "audio_1404.wav",
    "audio_176.wav",
    "audio_12.wav",
]

want = {n: None for n in TARGETS}
for s in sorted(glob.glob(os.path.join(DATA, "eval-*.parquet"))):
    t = pq.read_table(s, columns=["audio_file_name", "transcription", "audio"])
    for nm, tr, au in zip(
        t.column(0).to_pylist(), t.column(1).to_pylist(), t.column(2).to_pylist()
    ):
        if nm in want and want[nm] is None and au and au.get("bytes"):
            want[nm] = (decode_audio(au["bytes"]), tr)
    if all(v is not None for v in want.values()):
        break

m = Model(
    "google/gemma-4-12B-it",
    max_new_tokens=1024,
    prompt=VERBATIM_PROMPT,
    greedy=True,
    no_repeat_ngram_size=3,
)
print("model loaded (greedy, thinking=False, no_repeat_ngram=3)\n", flush=True)

for nm in TARGETS:
    if want[nm] is None:
        print(f"{nm}: NOT FOUND")
        continue
    audio, gt = want[nm]
    hyp = m.transcribe(audio)
    print(
        f"=== {nm} dur={len(audio)/SR:.0f}s  hyp_words={len(hyp.split())} ===",
        flush=True,
    )
    print("GT :", gt[:200].replace(chr(10), " "), flush=True)
    print("HYP:", hyp[:200].replace(chr(10), " ") if hyp else "(EMPTY)", flush=True)
    print(flush=True)
