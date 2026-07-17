"""QLoRA fine-tuning for Gemma 4 E*B on CoSHE-Eval sample7 (overfit smoke test).

4-bit NF4 base (double quant, bf16 compute) + LoRA on the text decoder only
(audio tower + multimodal embedders frozen). Plain HF Trainer with the custom
multimodal collator in data.py.

Usage (inside the gemma4 image venv):
    python train.py \
        --model google/gemma-4-E2B-it \
        --data_dir /data/coshe-eval/sample7 \
        --output_dir /train/out/e2b-overfit \
        --epochs 40 --lr 2e-4 --batch_size 1 --grad_accum 1
"""

import argparse

import torch
from data import CosheParquetDataset, CosheSample7Dataset, Gemma4AudioCollator
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForMultimodalLM,
    AutoProcessor,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

# LoRA scoped to the text decoder (language_model) attention + MLP projections.
# Regex (PEFT re.fullmatch) keeps it off the identically-named audio-tower linears.
# NOTE: an experiment adding lm_head + embed_tokens here (rank 64) made free-running
# generation DEGENERATE into "Speaker 1: Speaker 1:..." loops — the blocker is target
# grounding, not output capacity, so we keep the lean attention+MLP set.
LORA_TARGETS = (
    r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
)
# With --include_head, also adapt lm_head + input embeddings for more memorization
# capacity (safe now that generation uses use_cache=False; the earlier "degenerate"
# result with these was a cache-bug artifact, not a real failure).
LORA_TARGETS_HEAD = (
    r"(.*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
    r"|.*language_model\.embed_tokens|lm_head)"
)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E2B-it")
    p.add_argument("--data_dir", default="/data/coshe-eval/sample7")
    p.add_argument(
        "--parquet_glob",
        default="",
        help="if set, use full CoSHE parquet instead of sample7",
    )
    p.add_argument("--limit", type=int, default=0, help="cap #samples (parquet only)")
    p.add_argument(
        "--cache_path", default="", help="pickle cache for decoded parquet audio"
    )
    p.add_argument("--output_dir", default="/train/out/e2b-overfit")
    p.add_argument("--epochs", type=float, default=40.0)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--batch_size", type=int, default=1)
    p.add_argument("--grad_accum", type=int, default=1)
    p.add_argument("--max_seconds", type=float, default=30.0)
    p.add_argument("--target_max_chars", type=int, default=0)
    p.add_argument("--lora_r", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument("--lora_dropout", type=float, default=0.0)
    p.add_argument(
        "--optim",
        default="adamw_torch",
        help="e.g. adamw_8bit / paged_adamw_8bit to save VRAM",
    )
    p.add_argument(
        "--include_head", action="store_true", help="also LoRA lm_head + embed_tokens"
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="resume from latest checkpoint in output_dir",
    )
    return p.parse_args()


def main():
    args = parse_args()
    torch.manual_seed(0)

    print(f"Loading processor + 4-bit model: {args.model}", flush=True)
    processor = AutoProcessor.from_pretrained(args.model)

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        # Keep the multimodal towers / embedders / lm_head in bf16. The audio
        # tower's ClippableLinear calls torch.finfo(weight.dtype) for gradient
        # clipping, which breaks on 4-bit (uint8-stored) weights. We only train
        # LoRA on the text decoder anyway.
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

    # Manual k-bit prep (NOT peft's prepare_model_for_kbit_training): that helper
    # upcasts every non-4bit param to fp32, which makes the text embeddings fp32
    # while the (quant-skipped, bf16) audio tower stays bf16 -> Gemma4's
    # multimodal masked_scatter merge then errors on the dtype mismatch. Keeping
    # everything bf16 avoids that and matches the recipe's bf16-throughout advice.
    for p in model.parameters():
        p.requires_grad = False
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    model.enable_input_require_grads()

    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=LORA_TARGETS_HEAD if args.include_head else LORA_TARGETS,
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    if args.parquet_glob:
        dataset = CosheParquetDataset(
            args.parquet_glob,
            max_seconds=args.max_seconds,
            target_max_chars=args.target_max_chars,
            limit=args.limit,
            cache_path=args.cache_path,
        )
    else:
        dataset = CosheSample7Dataset(
            args.data_dir,
            max_seconds=args.max_seconds,
            target_max_chars=args.target_max_chars,
        )
    print(f"Dataset: {len(dataset)} samples", flush=True)
    collator = Gemma4AudioCollator(processor)

    targs = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        lr_scheduler_type="constant",
        warmup_steps=0,
        bf16=True,
        fp16=False,
        logging_steps=1,
        save_strategy="epoch",
        save_total_limit=2,
        optim=args.optim,
        report_to=[],
        remove_unused_columns=False,
        dataloader_num_workers=0,
        gradient_checkpointing=False,  # already enabled via prepare_model_for_kbit_training
    )

    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=dataset,
        data_collator=collator,
    )

    # Resume from the latest checkpoint in output_dir if present and --resume set.
    # /home is persisted on Jarvis Labs but the process dies on VM pause/restart, so
    # resuming from the last per-epoch checkpoint avoids wasting progress.
    import os as _os

    resume = False
    if args.resume and _os.path.isdir(args.output_dir):
        ckpts = [d for d in _os.listdir(args.output_dir) if d.startswith("checkpoint-")]
        resume = len(ckpts) > 0
    print(f"Starting training... (resume={resume})", flush=True)
    trainer.train(resume_from_checkpoint=resume)

    print(f"Saving adapter to {args.output_dir}", flush=True)
    trainer.save_model(args.output_dir)
    processor.save_pretrained(args.output_dir)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
