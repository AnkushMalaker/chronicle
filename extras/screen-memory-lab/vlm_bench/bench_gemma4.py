"""Benchmark gemma4 vision models as a screen-frame scene describer.

Runs on a GPU box with the gemma4 weights cached. Loads one model, then sweeps
a set of prompts over a set of exported frames, recording the output, the
latency and the token counts for each.

The point is not "can a VLM describe a screenshot" -- obviously it can. The
questions are:

1. Can it name the activity well enough to segment a day, given that no cheap
   deterministic signal can (measured: app names are empty for 36% of frames,
   motion cuts an 11-hour day 103 times, vocabulary drift 110 times)?
2. Can it spot a state announcement -- the thing typographic salience was
   reaching for -- on the ~80% of frames salience cannot rank at all because
   they have no `elements` rows of their own?
3. Can it tell when it needs more information, and ask for something the
   collector can actually supply? That is what makes the
   collector -> backend -> model -> collector loop possible instead of a fixed
   capture rate chosen by guesswork.
4. What does it cost per day at a given capture frequency?

Prompt 3 (`triage`) is the loop driver, so its `need` field is constrained to a
fixed vocabulary the backend can act on rather than free text.

Run:
    python bench_gemma4.py --model google/gemma-4-E2B-it \
        --frames frames/targeted --prompts describe,structured,triage,event \
        --out results/e2b_targeted.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import torch

NEED_VOCAB = [
    "nothing",
    "ocr_text",
    "neighbouring_frames",
    "higher_resolution",
    "later_frames",
    "window_title",
]

PROMPTS: dict[str, str] = {
    # Unconstrained. Establishes what we get for free, and how much of the
    # output is padding we would have to strip.
    "describe": (
        "This is a screenshot of a computer screen. Describe what is happening "
        "on it in two or three sentences. Say what application is in use and "
        "what the person appears to be doing."
    ),
    # The shape a backend would actually store.
    "structured": (
        "This is a screenshot of a computer screen.\n\n"
        "Return ONLY a JSON object, no prose, with these keys:\n"
        '  "activity": short phrase for what is happening (e.g. "playing a '
        'real-time strategy game", "watching a video", "editing code")\n'
        '  "application": the application or site, or null if unclear\n'
        '  "salient_text": the largest or most prominent text on screen, verbatim, '
        "or null\n"
        '  "is_state_announcement": true if the screen is announcing an outcome or '
        "state change (a result, a confirmation, an error, a completion) rather "
        "than showing ongoing activity\n"
        '  "entities": list of proper nouns visible that identify things (player '
        "names, map names, file names, repo names, video titles)\n"
        '  "confidence": 0.0 to 1.0\n'
    ),
    # The loop driver. Cheap gate plus an actionable request.
    "triage": (
        "This is one screenshot sampled from a continuous screen recording.\n\n"
        "Decide whether this frame is worth spending more analysis on, and "
        "whether you need more information to tell.\n\n"
        "Return ONLY a JSON object with these keys:\n"
        '  "worth_keeping": true if this frame shows something that would matter '
        "in a diary of the person's day (an outcome, a decision, a completed "
        "task, a notable event). false for idle screens, ongoing activity with "
        "no change, menus, and screensavers.\n"
        '  "why": one short clause\n'
        '  "need": exactly one of ' + json.dumps(NEED_VOCAB) + " -- what would "
        'most change your answer. Use "nothing" if you are already confident.\n'
        '  "confidence": 0.0 to 1.0\n'
    ),
    # Added after the first E2B run, which read "Victory"/"Defeat" as
    # salient_text and named both players, then answered
    # is_state_announcement=false and event=false with the reason "ongoing
    # activity or game progress screen". The AoE4 post-match screen genuinely
    # *is* a statistics screen with the outcome as its title, so asking the
    # model to classify the screen invites the wrong answer. These two prompts
    # test whether the failure is framing rather than capability: ask directly
    # for the outcome instead of asking what kind of screen it is.
    "outcome": (
        "This is a screenshot of a computer screen.\n\n"
        "Question: does this screen state the RESULT of something that has "
        "finished? Examples of result words: Victory, Defeat, Won, Lost, "
        "Passed, Failed, Complete, Delivered, Order placed, Merged, Error, "
        "Success, Cancelled.\n\n"
        "Look especially at the largest text on the screen.\n\n"
        "Return ONLY JSON:\n"
        '  {"states_a_result": true|false, "result_word": "<the word verbatim, '
        'or null>", "what_finished": "<short phrase, or null>", '
        '"who_or_what_it_applies_to": "<name, or null>", '
        '"other_names_visible": [<names>], "confidence": 0.0-1.0}\n\n'
        "A screen can state a result and also show statistics about it. If a "
        "result word is present, states_a_result is true even if the rest of "
        "the screen is a summary, a graph, or a table."
    ),
    # Same question again, but with no vocabulary supplied at all -- to check
    # the previous prompt is not just pattern-matching the example list.
    "concluded": (
        "This is a screenshot of a computer screen.\n\n"
        "Has something just concluded on this screen -- finished, succeeded, "
        "failed, been decided, or been confirmed? Or is this screen showing "
        "activity still in progress, a menu, or idle content?\n\n"
        "Return ONLY JSON:\n"
        '  {"something_concluded": true|false, "evidence": "<the text on '
        'screen that shows it, verbatim, or null>", "outcome": "<what the '
        'outcome was, or null>", "confidence": 0.0-1.0}\n'
    ),
    # Direct extraction, to compare against the text-only pipelines.
    "event": (
        "This is a screenshot of a computer screen.\n\n"
        "If this screen shows a completed event with an outcome, return ONLY a "
        "JSON object:\n"
        '  {"event": true, "kind": "<short type>", "outcome": "<what happened>", '
        '"participants": [<names>], "where": "<place/map/site or null>", '
        '"evidence": "<the text on screen that proves it>"}\n\n'
        "If it does not -- it is ongoing activity, a menu, a loading screen, or "
        "just someone reading -- return ONLY:\n"
        '  {"event": false, "reason": "<short clause>"}\n\n'
        "Important: text that merely *discusses* an event is not an event. A "
        "chat window or document describing a game result is not a game result.\n"
    ),
}

# Round 2 of the loop: what the backend sends back for each `need`.
FOLLOWUP = {
    "ocr_text": (
        "Here is the OCR text the capture pipeline stored for this same frame. "
        "It may be garbled, and it may include text from background windows that "
        "were not visible.\n\n---\n{ocr}\n---\n\n"
        "Now answer again, same JSON shape."
    ),
    "window_title": (
        "The window title recorded for this frame was: {window!r}\n"
        "The application name was: {app!r}\n\n"
        "Now answer again, same JSON shape."
    ),
    "neighbouring_frames": (
        "Here are the frames captured immediately before and after this one, in "
        "order. Use them to tell whether this frame is a change or a "
        "continuation.\n\nNow answer again, same JSON shape."
    ),
    "higher_resolution": (
        "Here is the same frame at higher resolution.\n\n"
        "Now answer again, same JSON shape."
    ),
    "later_frames": (
        "Here are frames captured after this one, in order.\n\n"
        "Now answer again, same JSON shape."
    ),
}


def load(model_id: str, dtype: str):
    # Imported lazily because transformers is a multi-gigabyte optional dep.
    from transformers import AutoModelForImageTextToText, AutoProcessor

    torch_dtype = {"bf16": torch.bfloat16, "fp16": torch.float16}[dtype]
    proc = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(
        model_id,
        dtype=torch_dtype,
        device_map="cuda",
        attn_implementation="sdpa",
    ).eval()
    return model, proc


def generate(model, proc, images: list[Path], prompt: str, max_new_tokens: int):
    content = [{"type": "image", "image": str(p)} for p in images]
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]

    inputs = proc.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(model.device)
    in_len = inputs["input_ids"].shape[-1]

    torch.cuda.synchronize()
    t0 = time.perf_counter()
    with torch.inference_mode():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
        )
    torch.cuda.synchronize()
    secs = time.perf_counter() - t0

    new = out[0][in_len:]
    text = proc.decode(new, skip_special_tokens=True).strip()
    return {
        "text": text,
        "input_tokens": int(in_len),
        "output_tokens": int(new.shape[-1]),
        "seconds": round(secs, 3),
    }


def parse_json(text: str):
    """Best-effort JSON extraction; records failure rather than hiding it."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        t = t[start : end + 1]
    try:
        return json.loads(t), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:160]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--frames", required=True, help="dir with png/ and manifest.json")
    ap.add_argument("--prompts", default="describe,structured,triage,event")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--max-new-tokens", type=int, default=320)
    ap.add_argument("--dtype", default="bf16", choices=["bf16", "fp16"])
    ap.add_argument(
        "--loop",
        action="store_true",
        help="run round 2 for triage frames that asked for more information",
    )
    args = ap.parse_args()

    root = Path(args.frames)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest = [m for m in manifest if m.get("png")]
    if args.max_frames:
        manifest = manifest[: args.max_frames]
    prompts = [p.strip() for p in args.prompts.split(",") if p.strip()]

    print(f"model={args.model}  frames={len(manifest)}  prompts={prompts}", flush=True)
    t_load = time.perf_counter()
    model, proc = load(args.model, args.dtype)
    print(
        f"loaded in {time.perf_counter()-t_load:.1f}s; "
        f"gpu_mem={torch.cuda.max_memory_allocated()/1e9:.1f}GB",
        flush=True,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("w")
    done = 0

    for m in manifest:
        png = root / "png" / m["png"]
        for name in prompts:
            rec = {
                "model": args.model,
                "prompt_name": name,
                "round": 1,
                "frame_id": m["frame_id"],
                "seq": m.get("seq"),
                "local": m.get("local"),
                "app": m.get("app"),
                "window": m.get("window"),
                "human_note": m.get("human_note"),
            }
            try:
                res = generate(model, proc, [png], PROMPTS[name], args.max_new_tokens)
                rec.update(res)
                if name != "describe":
                    parsed, err = parse_json(res["text"])
                    rec["parsed"] = parsed
                    rec["parse_error"] = err
            except Exception as exc:  # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            done += 1

            # ------------------------------------------------ round 2 of loop
            need = (rec.get("parsed") or {}).get("need")
            if not (args.loop and name == "triage" and need and need != "nothing"):
                continue
            if need not in FOLLOWUP:
                continue

            images = [png]
            extra = FOLLOWUP[need].format(
                ocr=m.get("ocr_text") or "(none stored)",
                window=m.get("window") or "",
                app=m.get("app") or "",
            )
            if need in ("neighbouring_frames", "later_frames"):
                sib = sorted((root / "png").glob("*.png"))
                idx = sib.index(png)
                if need == "neighbouring_frames":
                    images = sib[max(0, idx - 1) : idx + 2]
                else:
                    images = sib[idx : idx + 3]
                if len(images) < 2:
                    continue

            rec2 = {
                **{
                    k: v
                    for k, v in rec.items()
                    if k
                    in (
                        "model",
                        "prompt_name",
                        "frame_id",
                        "seq",
                        "local",
                        "app",
                        "window",
                        "human_note",
                    )
                },
                "round": 2,
                "asked_for": need,
                "images_supplied": len(images),
            }
            try:
                res2 = generate(
                    model,
                    proc,
                    images,
                    PROMPTS["triage"] + "\n\n" + extra,
                    args.max_new_tokens,
                )
                rec2.update(res2)
                parsed2, err2 = parse_json(res2["text"])
                rec2["parsed"] = parsed2
                rec2["parse_error"] = err2
                rec2["round1_worth"] = (rec.get("parsed") or {}).get("worth_keeping")
                rec2["round2_worth"] = (parsed2 or {}).get("worth_keeping")
            except Exception as exc:  # noqa: BLE001
                rec2["error"] = f"{type(exc).__name__}: {exc}"[:400]
            fh.write(json.dumps(rec2) + "\n")
            fh.flush()
            done += 1

        if (manifest.index(m) + 1) % 5 == 0:
            print(
                f"  {manifest.index(m)+1}/{len(manifest)} frames, {done} calls",
                flush=True,
            )

    fh.close()
    print(f"wrote {done} records to {out_path}", flush=True)
    print(f"peak gpu mem {torch.cuda.max_memory_allocated()/1e9:.1f}GB", flush=True)


if __name__ == "__main__":
    main()
