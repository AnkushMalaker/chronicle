"""Caption archive frames with codex, straight from the archive.

Report 10 measured codex as a captioner: it takes images (`-i/--image`, which is
variadic, so the prompt must be piped), sweeps all five attribution traps on both
`sol` and `terra`, and costs ~1.9k marginal tokens and ~7 s per frame.

**This is a substitute, not the production configuration.** Report 10 §4 argues
the bulk caption index belongs on a LOCAL model (gemma4-E2B) because stage 1
touches every frame of the day and codex sends them off-box. This script exists
so a held-out window can be indexed without standing up a GPU, and any number it
produces should be read as an *upper* bound on what the local model would give
(codex writes better captions than E2B -- report 10 §2).

Emits the same JSONL schema as `caption_frames.py` / `caption_cloud.py` so
`localise.py` and `retrieve_eval.py` can read it unchanged.

    uv run python -m vlm_bench.caption_codex \\
        --start 2026-07-25T18:00:00+00:00 --end 2026-07-25T23:30:00+00:00 \\
        --every 120 --out out/vlm/captions_heldout_codex.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from lab.spipe import Archive
from vlm_bench.caption_prompts import PROMPTS

# `codex exec` puts the CLEAN answer on stdout and the whole transcript (banner,
# echoed prompt, answer again, token report) on stderr. Capturing them merged --
# which is what `2>&1` at a shell prompt does -- makes the answer look like it
# needs extracting from a transcript. It does not: read stdout.
TOKENS = re.compile(r"^tokens used$\s*^([\d,]+)$", re.S | re.M)


def grid(arch: Archive, start: str, end: str, every_s: int) -> list:
    """Frames on a blind clock grid -- chosen by the clock, never by content.

    Report 08's convention, kept deliberately: a systematic grid is what stops a
    good score coming from having picked flattering frames.
    """
    lo = datetime.fromisoformat(start)
    hi = datetime.fromisoformat(end)
    frames = [f for f in arch.frames(start, end) if f.chunk_path]
    picked, target = [], lo
    while target <= hi:
        near = min(
            frames,
            key=lambda f: abs((f.timestamp - target).total_seconds()),
            default=None,
        )
        if (
            near is not None
            and abs((near.timestamp - target).total_seconds()) <= every_s / 2
        ):
            if not picked or picked[-1].id != near.id:
                picked.append(near)
        target += timedelta(seconds=every_s)
    return picked


def caption(png: Path, prompt: str, model: str, timeout: int = 180) -> tuple[str, int]:
    proc = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", "-m", model, "-i", str(png)],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    text = proc.stdout.strip()
    t = TOKENS.search(proc.stderr)
    return text, int(t.group(1).replace(",", "")) if t else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--every", type=int, default=120, help="grid spacing, seconds")
    ap.add_argument("--prompt", default="caption", help="report 08 §6.2: use `caption`")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--set", dest="setname", default="heldout")
    args = ap.parse_args()

    arch = Archive()
    frames = grid(arch, args.start, args.end, args.every)
    print(
        f"{len(frames)} frames on a 1/{args.every}s grid "
        f"({args.start} -> {args.end})",
        file=sys.stderr,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    done: set[int] = set()
    if args.out.exists():  # resumable; a 300-frame run should survive a hiccup
        for line in args.out.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("prompt") == args.prompt:
                    done.add(int(rec["frame_id"]))
        print(f"resuming: {len(done)} already captioned", file=sys.stderr)

    prompt = PROMPTS[args.prompt]
    t0, n_ok, n_err, toks = time.time(), 0, 0, 0
    with args.out.open("a") as fh:
        for i, f in enumerate(frames, 1):
            if f.id in done:
                continue
            try:
                png = arch.frame_png(f.id)
                text, used = caption(png, prompt, args.model)
                ok = bool(text)
            except Exception as exc:
                text, used, ok = f"{type(exc).__name__}: {exc}", 0, False
            n_ok, n_err, toks = n_ok + ok, n_err + (not ok), toks + used
            fh.write(
                json.dumps(
                    {
                        "set": args.setname,
                        "frame_id": f.id,
                        "prompt": args.prompt,
                        "model": args.model,
                        "ok": ok,
                        "text": text if ok else "",
                        "error": None if ok else text,
                        "utc": f.timestamp.isoformat(),
                        "local": f.local_time.isoformat(),
                        "ocr_chars": len(f.text),
                        "tokens": used,
                    }
                )
                + "\n"
            )
            fh.flush()
            if i % 10 == 0:
                el = time.time() - t0
                print(
                    f"  {i}/{len(frames)}  ok={n_ok} err={n_err}  "
                    f"{el/max(i,1):.1f}s/frame  {toks:,} tok",
                    file=sys.stderr,
                )

    print(
        f"done: {n_ok} ok, {n_err} errors, {toks:,} tokens, "
        f"{time.time()-t0:.0f}s -> {args.out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
