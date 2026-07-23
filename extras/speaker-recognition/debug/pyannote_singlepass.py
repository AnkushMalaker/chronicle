"""Run pyannote (the repo's diarization model) SINGLE-PASS on a whole file — no chunking.
Compares against the service's 60s-chunked path. Run inside the speaker-service container."""
import json
import os
import sys

import torch
from pyannote.audio import Pipeline

wav, out = sys.argv[1], sys.argv[2]
pipe = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-community-1", token=os.environ.get("HF_TOKEN")
)
pipe.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
output = pipe(wav)  # whole file, one pass — global speaker clustering
diar = output.speaker_diarization if hasattr(output, "speaker_diarization") else output
diar = diar.support(collar=2.0)  # same gap-fill the repo applies
segs = [{"start": float(t.start), "end": float(t.end), "speaker": str(spk)}
        for t, _, spk in diar.itertracks(yield_label=True)]
json.dump({"segments": segs, "provider": "pyannote_truesinglepass"}, open(out, "w"))
print(f"{len(segs)} segs, {len({s['speaker'] for s in segs})} speakers -> {out}", flush=True)
