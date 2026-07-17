"""Diagnose why teacher-forced loss -> 0 but generation doesn't reproduce targets.

Checks two things on the trained v3 adapter (target_max_chars=540):

  1. TOKEN-FORMAT PARITY: does the training collator's input construction
     (apply_chat_template(tokenize=False) -> processor(text=str, audio=)) produce
     the SAME prompt token ids as the inference path
     (apply_chat_template(tokenize=True, return_dict=True))?  A mismatch (e.g. a
     duplicated <bos>) would mean we train on one format and generate on another.

  2. TEACHER-FORCED REPRODUCTION: feed the full (prompt+target) exactly as in
     training, take argmax of the logits at each SUPERVISED position, and measure
     how many match the gold target token. If this is ~100%, the model truly
     memorized and any failure is in the generation path; if it's low, loss never
     really reached 0 on the hard (early) tokens.
"""

import argparse

import torch
from data import DEFAULT_PROMPT, CosheSample7Dataset, Gemma4AudioCollator
from peft import PeftModel
from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E2B-it")
    p.add_argument("--adapter", default="/train/out/e2b-overfit-v3")
    p.add_argument("--data_dir", default="/data/coshe-eval/sample7")
    p.add_argument("--target_max_chars", type=int, default=540)
    return p.parse_args()


def main():
    args = parse_args()
    processor = AutoProcessor.from_pretrained(args.model)
    tok = processor.tokenizer

    ds = CosheSample7Dataset(
        args.data_dir, max_seconds=30.0, target_max_chars=args.target_max_chars
    )
    item = ds[0]

    # ---- 1. TOKEN-FORMAT PARITY ----
    print("===== 1. TOKEN-FORMAT PARITY (prompt only) =====", flush=True)
    user_turn = {
        "role": "user",
        "content": [
            {"type": "text", "text": DEFAULT_PROMPT},
            {"type": "audio", "audio": item["audio"]},
        ],
    }
    # training-style: tokenize=False -> processor(text=str, audio=)
    ptext = processor.apply_chat_template(
        [user_turn], tokenize=False, add_generation_prompt=True
    )
    train_path = processor(text=[ptext], audio=[item["audio"]], return_tensors="pt")
    train_ids = train_path["input_ids"][0]
    # inference-style: tokenize=True, return_dict=True
    infer_path = processor.apply_chat_template(
        [user_turn],
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
        add_generation_prompt=True,
    )
    infer_ids = infer_path["input_ids"][0]

    print(
        f"  train-path  len={len(train_ids)}  first8={train_ids[:8].tolist()}",
        flush=True,
    )
    print(
        f"  infer-path  len={len(infer_ids)}  first8={infer_ids[:8].tolist()}",
        flush=True,
    )
    bos = tok.bos_token_id
    print(
        f"  bos_token_id={bos}  train leading bos count={int((train_ids[:3]==bos).sum())}  "
        f"infer leading bos count={int((infer_ids[:3]==bos).sum())}",
        flush=True,
    )
    same = len(train_ids) == len(infer_ids) and bool((train_ids == infer_ids).all())
    print(f"  >>> IDENTICAL: {same}", flush=True)
    if not same:
        # show first divergence
        n = min(len(train_ids), len(infer_ids))
        diff = (train_ids[:n] != infer_ids[:n]).nonzero()
        first = int(diff[0]) if len(diff) else n
        print(f"  first divergence at pos {first}", flush=True)
        print(
            f"    train[{first}:{first+6}]={train_ids[first:first+6].tolist()} "
            f"-> {tok.convert_ids_to_tokens(train_ids[first:first+6].tolist())}",
            flush=True,
        )
        print(
            f"    infer[{first}:{first+6}]={infer_ids[first:first+6].tolist()} "
            f"-> {tok.convert_ids_to_tokens(infer_ids[first:first+6].tolist())}",
            flush=True,
        )

    # ---- 2. TEACHER-FORCED REPRODUCTION ----
    print(
        "\n===== 2. TEACHER-FORCED ARGMAX REPRODUCTION (v3 adapter) =====", flush=True
    )
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        llm_int8_skip_modules=[
            "model.audio_tower",
            "model.vision_tower",
            "model.embed_audio",
            "model.embed_vision",
            "lm_head",
        ],
    )
    model = AutoModelForMultimodalLM.from_pretrained(
        args.model,
        quantization_config=bnb,
        dtype=torch.bfloat16,
        device_map="auto",
        attn_implementation="eager",
    )
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter)
    model.eval()

    collator = Gemma4AudioCollator(processor)
    item0 = ds[0]
    batch = collator([item0])
    batch = {k: v.to(model.device) for k, v in batch.items()}
    with torch.inference_mode():
        out_with_labels = model(**batch)  # model's OWN loss (its internal shift)
        out = model(**{k: v for k, v in batch.items() if k != "labels"})
    logits = out.logits[0]
    labels = batch["labels"][0]
    # my manual shift: logits[t] predicts token t+1
    pred = logits[:-1].argmax(-1)
    gold = labels[1:]
    sup = gold != -100
    manual_loss = torch.nn.functional.cross_entropy(
        logits[:-1][sup].float(), gold[sup], reduction="mean"
    )
    # NO-shift variant: logits[t] vs labels[t] (in case model pre-shifts)
    sup0 = labels != -100
    noshift_loss = torch.nn.functional.cross_entropy(
        logits[sup0].float(), labels[sup0], reduction="mean"
    )
    print(
        f"  model.forward(labels=...).loss = {float(out_with_labels.loss):.4f}",
        flush=True,
    )
    print(f"  my manual SHIFTED loss         = {float(manual_loss):.4f}", flush=True)
    print(f"  my manual NO-shift loss        = {float(noshift_loss):.4f}", flush=True)

    # with vs without adapter (model's own loss)
    if args.adapter:
        with torch.inference_mode(), model.disable_adapter():
            base_loss = float(model(**batch).loss)
        print(
            f"  model loss WITH adapter={float(out_with_labels.loss):.4f}  "
            f"WITHOUT adapter (base)={base_loss:.4f}",
            flush=True,
        )

    print("\n  per-sample argmax-match (shifted):", flush=True)
    for item in ds:
        b = collator([item])
        b = {k: v.to(model.device) for k, v in b.items()}
        with torch.inference_mode():
            o = model(**{k: v for k, v in b.items() if k != "labels"})
        lg = o.logits[0]
        lb = b["labels"][0]
        p = lg[:-1].argmax(-1)
        g = lb[1:]
        s = g != -100
        n = int(s.sum())
        m = int((p[s] == g[s]).sum())
        print(f"  {item['name']:16s} match={m}/{n} ({100*m/max(n,1):.1f}%)", flush=True)


if __name__ == "__main__":
    main()
