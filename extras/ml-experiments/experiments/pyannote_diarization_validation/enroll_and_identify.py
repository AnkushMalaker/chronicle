"""Validate the PRODUCTION diarization path: enroll speakers, then /diarize-and-identify, and
score DER on the IDENTIFIED labels (identified_as) — which is what the backend actually stores.

This tests whether per-segment centroid matching reconciles the cross-chunk label inconsistency
(the user's point). Enrollment segments are EXCLUDED from scoring via a held-out UEM, so it's not
train-on-test. Throwaway user_id=9999; speakers deleted by the caller afterward.
"""

import io
import json
import sys
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

SVC = "http://localhost:8085"
SR = 16000
USER = 9999
N_ENROLL = 3  # segments per speaker used for enrollment
MIN_ENROLL_S = 2.5  # only use segments at least this long for enrollment


def parse_rttm(p: Path):
    by_spk = {}
    for ln in p.read_text().splitlines():
        f = ln.split()
        if f and f[0] == "SPEAKER":
            start, dur, spk = float(f[3]), float(f[4]), f[7]
            by_spk.setdefault(spk, []).append((start, start + dur))
    return by_spk


def main():
    meeting = sys.argv[1]
    wav = Path(f"data/ami_sdm_slice/audio/{meeting}.wav")
    ref = Path(f"data/ami_sdm_slice/ref_rttm/{meeting}.rttm")
    outdir = Path("experiments/pyannote_diarization_validation/enrolled")
    outdir.mkdir(parents=True, exist_ok=True)
    uemdir = outdir / "uem"
    uemdir.mkdir(exist_ok=True)
    hypdir = outdir / f"{meeting}_results"
    hypdir.mkdir(exist_ok=True)

    audio, sr = sf.read(wav, dtype="float32")
    assert sr == SR
    by_spk = parse_rttm(ref)

    enroll_ranges = []  # (start,end) excluded from scoring
    for spk, segs in by_spk.items():
        longest = sorted(segs, key=lambda s: s[1] - s[0], reverse=True)
        picked = [s for s in longest if s[1] - s[0] >= MIN_ENROLL_S][:N_ENROLL]
        if not picked:
            picked = longest[:N_ENROLL]
        clip = np.concatenate([audio[int(s * SR) : int(e * SR)] for s, e in picked])
        enroll_ranges += picked
        buf = io.BytesIO()
        sf.write(buf, clip, SR, format="WAV", subtype="PCM_16")
        buf.seek(0)
        sid = f"{meeting}_{spk}"
        r = requests.post(
            f"{SVC}/enroll/upload",
            files={"file": ("enroll.wav", buf, "audio/wav")},
            data={"speaker_id": sid, "speaker_name": spk, "user_id": str(USER)},
            timeout=120,
        )
        print(
            f"enroll {spk}: {r.status_code} ({sum(e-s for s,e in picked):.1f}s)",
            flush=True,
        )

    # held-out UEM = whole meeting minus enrolled ranges
    total = len(audio) / SR
    ranges = sorted(enroll_ranges)
    held, cur = [], 0.0
    for s, e in ranges:
        if s > cur:
            held.append((cur, s))
        cur = max(cur, e)
    if cur < total:
        held.append((cur, total))
    (uemdir / f"{meeting}.uem").write_text(
        "\n".join(f"{meeting} 1 {s:.3f} {e:.3f}" for s, e in held) + "\n"
    )

    # diarize + identify with enrolled speakers
    with open(wav, "rb") as f:
        r = requests.post(
            f"{SVC}/diarize-and-identify",
            files={"file": (wav.name, f, "audio/wav")},
            data={
                "user_id": str(USER),
                "min_duration": 0.5,
                "collar": 2.0,
                "min_duration_off": 1.5,
            },
            timeout=1800,
        )
    segs = r.json().get("segments", [])
    n_id = sum(1 for s in segs if s.get("identified_as"))
    print(
        f"{meeting}: {len(segs)} segs, {n_id} identified, "
        f"{len({s['speaker'] for s in segs})} diar-labels",
        flush=True,
    )

    # hyp speaker = identified_as (production label) else fall back to diar label
    hyp = [
        {
            "start": s["start"],
            "end": s["end"],
            "speaker": s.get("identified_as") or f"diar_{s.get('speaker')}",
        }
        for s in segs
    ]
    json.dump(
        {"segments": hyp, "provider": "enrolled_identified"},
        open(hypdir / f"{meeting}.pyannote.json", "w"),
    )
    print(
        f"wrote {hypdir}/{meeting}.pyannote.json + held-out uem ({len(held)} regions)",
        flush=True,
    )


if __name__ == "__main__":
    main()
