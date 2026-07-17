"""LoRA finetune of Gemma4-E2B on the HONEST windowed CoSHE set (20% train / 10% val),
val-loss early stopping. Successor to train_lora_split.py: instead of the "first-30s +
proportional char-truncation" hack, it trains on real <=30s (audio, GT-text) windows cut
at forced-alignment word timings (build_windowed_dataset.py). The held-out 70% test clips
are never touched here; they are scored separately via the windowed-stitch path.

decoder-only vs --include_head as before. Same low constant LR + LoRA recipe.
"""

import argparse
import json
import os

import torch
from data import Gemma4AudioCollator
from data_windowed import PLAIN_PROMPT, WindowedManifestDataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForMultimodalLM,
    AutoProcessor,
    BitsAndBytesConfig,
    EarlyStoppingCallback,
    Trainer,
    TrainingArguments,
)

LORA_TARGETS_HEAD = (
    r"(.*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
    r"|.*language_model\.embed_tokens|lm_head)"
)
LORA_TARGETS = (
    r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E2B-it")
    p.add_argument("--manifest", default="/home/coshe_windowed/manifest.jsonl")
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
        "--attn",
        default="sdpa",
        help="sdpa matches prod/eval (gemma4 default); eager was the old buggy-at-eval choice",
    )
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
        attn_implementation=args.attn,
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

    train_ds = WindowedManifestDataset(args.manifest, "train")
    val_ds = WindowedManifestDataset(args.manifest, "val")
    print(f"train windows={len(train_ds)}  val windows={len(val_ds)}", flush=True)
    collator = Gemma4AudioCollator(proc, prompt=PLAIN_PROMPT)

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
        train_dataset=train_ds,
        eval_dataset=val_ds,
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
    print(
        f"DONE  best_checkpoint={trainer.state.best_model_checkpoint}  "
        f"best_eval_loss={trainer.state.best_metric}",
        flush=True,
    )


if __name__ == "__main__":
    main()
