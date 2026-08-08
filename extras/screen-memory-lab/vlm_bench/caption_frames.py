"""Caption screen frames with a local VLM so they can be RETRIEVED, not asserted.

This is the tier-1 job from docs/research/screen-memory/07-how-real-systems-do-it.md.
The model is not asked whether anything happened, who did it, or whether it
concluded. It is asked to describe what is on the screen, because a description
is true regardless of whose screen it is -- which is the claim report 07 makes and
this script exists to test.

Two prompts, both pure description, deliberately carrying NO authorship or
attribution rule. If report 07 is right, captions of the known traps (my own
terminal, the review viewer I built, another player's profile page) will come back
describing a terminal, a viewer and a profile page without being told to.
Baking the rule in would make that untestable.

Run on the GPU box:
    python caption_frames.py --model google/gemma-4-E2B-it \
        --dirs frames/cap_w1,frames/cap_w2,frames/cap_w3,frames/cap_known \
        --out captions_e2b.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import torch
from transformers import AutoModelForImageTextToText, AutoProcessor
from vlm_bench.caption_prompts import PROMPTS


def load(model_id: str):
    proc = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(
        model_id,
        dtype=torch.bfloat16,
        device_map="cuda",
        attn_implementation="sdpa",
    ).eval()
    return proc, model


def caption(proc, model, png: Path, prompt: str, max_new_tokens: int) -> dict:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "url": str(png)},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    inputs = proc.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(model.device)
    n_in = int(inputs["input_ids"].shape[-1])
    t0 = time.time()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    text = proc.decode(out[0][n_in:], skip_special_tokens=True).strip()
    return {
        "text": text,
        "input_tokens": n_in,
        "output_tokens": int(out.shape[-1]) - n_in,
        "seconds": round(time.time() - t0, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemma-4-E2B-it")
    ap.add_argument("--dirs", required=True, help="comma-separated frame dirs")
    ap.add_argument("--prompts", default="caption,caption_text")
    ap.add_argument("--max-new-tokens", type=int, default=260)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    jobs = []
    for d in args.dirs.split(","):
        root = Path(d.strip())
        manifest = json.loads((root / "manifest.json").read_text())
        for m in manifest:
            if not m.get("png"):
                continue
            jobs.append((root.name, root / "png" / m["png"], m))
    prompts = [p.strip() for p in args.prompts.split(",")]
    print(
        f"{len(jobs)} frames x {len(prompts)} prompts = {len(jobs)*len(prompts)} calls"
    )

    print(f"loading {args.model} ...", flush=True)
    t0 = time.time()
    proc, model = load(args.model)
    print(f"loaded in {time.time()-t0:.0f}s", flush=True)

    out = Path(args.out)
    done = 0
    with out.open("w") as fh:
        for pname in prompts:
            for setname, png, meta in jobs:
                try:
                    res = caption(proc, model, png, PROMPTS[pname], args.max_new_tokens)
                    row = {"ok": True, **res}
                except Exception as exc:  # noqa: BLE001 - record and continue
                    row = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                row.update(
                    {
                        "set": setname,
                        "prompt": pname,
                        "frame_id": meta["frame_id"],
                        "utc": meta.get("utc"),
                        "local": meta.get("local"),
                        "png": png.name,
                        "ocr_chars": len(meta.get("ocr_text") or ""),
                    }
                )
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                done += 1
                if done % 20 == 0:
                    print(f"  {done}/{len(jobs)*len(prompts)}", flush=True)

    print(f"wrote {done} rows -> {out}")


if __name__ == "__main__":
    main()
