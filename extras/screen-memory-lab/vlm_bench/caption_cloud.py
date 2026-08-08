"""Caption the same frames with a cloud VLM, for one comparison against E2B.

Writes the identical JSONL shape as caption_frames.py so vlm_bench/retrieve_eval.py
scores both without changes. Same two prompts, same frames, same absence of any
authorship rule -- the only variable is the model.

Run:
    uv run python vlm_bench/caption_cloud.py \
        --model google/gemini-3-flash-preview \
        --dirs out/frames/cap_w1,out/frames/cap_w2,out/frames/cap_w3,out/frames/cap_known \
        --out out/vlm/captions_gemini.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.llm import LLM  # noqa: E402
from vlm_bench.caption_prompts import PROMPTS  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemini-3-flash-preview")
    ap.add_argument("--dirs", required=True)
    ap.add_argument("--prompts", default="caption,caption_text")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="cap frames per set, 0 = all")
    ap.add_argument(
        "--workers",
        type=int,
        default=8,
        help="concurrent requests; a single 1280px screenshot round-trips in "
        "~15s, so serial is an hour and a half for this set",
    )
    args = ap.parse_args()

    jobs = []
    for d in args.dirs.split(","):
        root = Path(d.strip())
        manifest = json.loads((root / "manifest.json").read_text())
        rows = [m for m in manifest if m.get("png")]
        if args.limit:
            rows = rows[: args.limit]
        for m in rows:
            jobs.append((root.name, root / "png" / m["png"], m))

    prompts = [p.strip() for p in args.prompts.split(",")]
    total = len(jobs) * len(prompts)
    print(
        f"{len(jobs)} frames x {len(prompts)} prompts = {total} calls -> {args.model}"
    )

    llm = LLM(model=args.model)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    work = [(p, s, png, m) for p in prompts for (s, png, m) in jobs]

    def one(item) -> dict:
        pname, setname, png, meta = item
        t0 = time.time()
        try:
            res = llm.complete(
                system="You describe screenshots factually and concisely.",
                prompt=PROMPTS[pname],
                images=[png],
                max_output_tokens=400,
            )
            row = {
                "ok": True,
                "text": (res.get("text") or "").strip(),
                "seconds": round(time.time() - t0, 2),
            }
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
        return row

    done = 0
    lock = threading.Lock()
    with out.open("w") as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(one, work):
            with lock:
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{total}  ${llm.usage.cost_usd:.2f}", flush=True)

    print(json.dumps(llm.usage.summary(), indent=1))
    print(f"wrote {done} rows -> {out}")


if __name__ == "__main__":
    main()
