"""Reproduce pyannote/speaker-diarization-community-1's published AMI-SDM DER (19.9%).

Their protocol: full SDM test files, single-pass (no chunking), RAW pipeline output, scored with
pyannote.metrics DiarizationErrorRate(collar=0, skip_overlap=False) against the only_words
reference + UEM, POOLED over the test set. Run inside the speaker-service container.

  python /app/debug/pyannote_ami_repro.py --audio-dir <d> --ref-dir <d> --uem-dir <d> [--support]
"""
import argparse
import glob
import os
from pathlib import Path

import torch
from pyannote.audio import Pipeline
from pyannote.database.util import load_rttm, load_uem
from pyannote.metrics.diarization import DiarizationErrorRate

ap = argparse.ArgumentParser()
ap.add_argument("--audio-dir", required=True)
ap.add_argument("--ref-dir", required=True)
ap.add_argument("--uem-dir", required=True)
ap.add_argument("--support", action="store_true", help="apply .support(collar=2.0) like the repo service does")
args = ap.parse_args()

meetings = sorted(Path(p).stem for p in glob.glob(f"{args.audio_dir}/*.wav"))
print(f"meetings ({len(meetings)}): {meetings}", flush=True)

pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=os.environ.get("HF_TOKEN"))
pipe.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))

metric = DiarizationErrorRate(collar=0.0, skip_overlap=False)
agg = {"confusion": 0.0, "missed detection": 0.0, "false alarm": 0.0, "total": 0.0}
for m in meetings:
    ref = load_rttm(f"{args.ref_dir}/{m}.rttm")[m]
    uem = load_uem(f"{args.uem_dir}/{m}.uem")[m]
    out = pipe(f"{args.audio_dir}/{m}.wav")
    hyp = out.speaker_diarization if hasattr(out, "speaker_diarization") else out
    if args.support:
        hyp = hyp.support(collar=2.0)
    d = metric(ref, hyp, uem=uem, detailed=True)
    for k in agg:
        agg[k] += d[k]
    print(f"  {m}: DER={d['diarization error rate']*100:5.1f}%  "
          f"miss={d['missed detection']:.0f} fa={d['false alarm']:.0f} conf={d['confusion']:.0f} "
          f"nspk_hyp={len(hyp.labels())}", flush=True)

pooled = (agg["confusion"] + agg["missed detection"] + agg["false alarm"]) / agg["total"]
print(f"\n=== POOLED DER = {pooled*100:.2f}%  "
      f"(miss {agg['missed detection']/agg['total']*100:.1f} / "
      f"fa {agg['false alarm']/agg['total']*100:.1f} / "
      f"conf {agg['confusion']/agg['total']*100:.1f})  over {len(meetings)} meetings ===", flush=True)
print(f"published community-1 AMI-SDM = 19.9%", flush=True)
