"""Split-aware LoRA finetune of Gemma4-E4B on CoSHE — a *generalization* experiment.

Unlike `train_until_wer.py` (which memorizes the full set to <2% WER), this trains on a
random 20% slice and selects the checkpoint with the lowest **validation loss** on a
held-out 10% slice. The remaining 70% (the test split) is never touched here — it is
scored separately for WER against the base-model baseline.

Split comes from a JSON file ({"train":[names], "val":[names], "test":[names]}) so the
exact same partition is used here (training) and locally (scoring). Items are matched to
the decoded-audio cache by `name` (== audio_file_name).

Strategy (per the user's spec): very low constant LR, LoRA adapter, early-stop when the
validation loss stops improving (HF EarlyStoppingCallback + load_best_model_at_end on
eval_loss). Two configs are supported via --include_head:
  * decoder-only  : LoRA on language_model q/k/v/o/gate/up/down (audio tower frozen)
  * include_head  : the above + LoRA on embed_tokens and lm_head
"""

import argparse
import json
import os

import torch
from data import CosheParquetDataset, Gemma4AudioCollator
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForMultimodalLM,
    AutoProcessor,
    BitsAndBytesConfig,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)
from window_target import apply_window_truncation

LORA_TARGETS_HEAD = (
    r"(.*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
    r"|.*language_model\.embed_tokens|lm_head)"
)
LORA_TARGETS = (
    r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
)


class ListDataset(torch.utils.data.Dataset):
    def __init__(self, items):
        self.items = items

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E4B-it")
    p.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
    p.add_argument("--cache_path", default="/home/gemma4ft/out/coshe_full_cache.pkl")
    p.add_argument("--split_file", default="/home/gemma4ft/split_20_10_70.json")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--epochs", type=float, default=40.0)
    p.add_argument("--lr", type=float, default=1e-5)
    p.add_argument("--batch_size", type=int, default=2)
    p.add_argument("--grad_accum", type=int, default=2)
    p.add_argument("--eval_batch_size", type=int, default=4)
    p.add_argument("--lora_r", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument("--include_head", action="store_true")
    p.add_argument("--optim", default="adamw_8bit")
    p.add_argument("--patience", type=int, default=4)
    p.add_argument(
        "--window_seconds",
        type=float,
        default=30.0,
        help="proportionally truncate target to this audio window (0=full)",
    )
    p.add_argument("--durations", default="/home/gemma4ft/durations.json")
    return p.parse_args()


def main():
    args = parse_args()
    torch.manual_seed(0)
    proc = AutoProcessor.from_pretrained(args.model)
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
    model.config.use_cache = False
    for pm in model.parameters():
        pm.requires_grad = False
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    model.enable_input_require_grads()
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=0.0,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=LORA_TARGETS_HEAD if args.include_head else LORA_TARGETS,
        ),
    )
    model.print_trainable_parameters()

    split = json.load(open(args.split_file))
    ds = CosheParquetDataset(
        args.parquet_glob,
        max_seconds=30.0,
        target_max_chars=0,
        cache_path=args.cache_path,
    )
    apply_window_truncation(list(ds), args.durations, args.window_seconds)
    by = {it["name"]: it for it in ds}
    train_items = [by[n] for n in split["train"] if n in by]
    val_items = [by[n] for n in split["val"] if n in by]
    missing_tr = len(split["train"]) - len(train_items)
    missing_va = len(split["val"]) - len(val_items)
    print(
        f"train={len(train_items)} (missing {missing_tr})  "
        f"val={len(val_items)} (missing {missing_va})  "
        f"test(held out)={len(split['test'])}",
        flush=True,
    )
    collator = Gemma4AudioCollator(proc)

    targs = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        per_device_eval_batch_size=args.eval_batch_size,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        lr_scheduler_type="constant",
        warmup_steps=0,
        bf16=True,
        fp16=False,
        logging_steps=10,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        optim=args.optim,
        report_to=[],
        remove_unused_columns=False,
        dataloader_num_workers=0,
        gradient_checkpointing=False,
        seed=0,
        data_seed=0,
    )

    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=ListDataset(train_items),
        eval_dataset=ListDataset(val_items),
        data_collator=collator,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=args.patience)],
    )

    print("Starting training...", flush=True)
    trainer.train()
    trainer.save_model(args.output_dir)
    proc.save_pretrained(args.output_dir)
    json.dump(
        trainer.state.log_history,
        open(os.path.join(args.output_dir, "log_history.json"), "w"),
    )
    # surface the chosen (best) checkpoint + its val loss for the writeup
    best = getattr(trainer.state, "best_model_checkpoint", None)
    best_loss = getattr(trainer.state, "best_metric", None)
    print(f"DONE  best_checkpoint={best}  best_eval_loss={best_loss}", flush=True)


if __name__ == "__main__":
    main()
