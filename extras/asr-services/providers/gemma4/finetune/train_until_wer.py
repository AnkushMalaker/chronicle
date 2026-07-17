"""Overfit CoSHE, evaluating WER every N epochs and stopping when corpus WER < target.

transformers 5.7 has the use_cache bug fixed, so eval uses fast cached generation.
Every `--eval_every` epochs: eval a fast subset; if subset WER < target, eval the
FULL set and stop only if that's also < target. Saves the adapter on stop.
"""

import argparse
import time

import jiwer
import torch
from data import DEFAULT_PROMPT, CosheParquetDataset, Gemma4AudioCollator
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import (
    AutoModelForMultimodalLM,
    AutoProcessor,
    BitsAndBytesConfig,
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

LORA_TARGETS_HEAD = (
    r"(.*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
    r"|.*language_model\.embed_tokens|lm_head)"
)
LORA_TARGETS = (
    r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
)
_WER_NORM = jiwer.Compose(
    [
        jiwer.ToLowerCase(),
        jiwer.RemovePunctuation(),
        jiwer.RemoveMultipleSpaces(),
        jiwer.Strip(),
        jiwer.ReduceToListOfListOfWords(),
    ]
)


def corpus_wer(refs, hyps):
    return jiwer.wer(
        refs, hyps, reference_transform=_WER_NORM, hypothesis_transform=_WER_NORM
    )


@torch.inference_mode()
def eval_wer(model, proc, items, batch_size, max_new_tokens):
    model.eval()
    prev_uc = model.config.use_cache
    model.config.use_cache = True
    proc.tokenizer.padding_side = "left"
    refs, hyps = [], []
    for s in range(0, len(items), batch_size):
        batch = items[s : s + batch_size]
        texts, audios = [], []
        for it in batch:
            msgs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DEFAULT_PROMPT},
                        {"type": "audio", "audio": it["audio"]},
                    ],
                }
            ]
            texts.append(
                proc.apply_chat_template(
                    msgs, tokenize=False, add_generation_prompt=True
                )
            )
            audios.append(it["audio"])
        inp = proc(text=texts, audio=audios, return_tensors="pt", padding=True).to(
            model.device
        )
        out = model.generate(
            **inp, max_new_tokens=max_new_tokens, do_sample=False, use_cache=True
        )
        for i, it in enumerate(batch):
            hyps.append(
                proc.decode(
                    out[i][inp["input_ids"].shape[-1] :], skip_special_tokens=True
                ).strip()
            )
            refs.append(it["target"])
    model.config.use_cache = prev_uc
    model.train()
    return corpus_wer(refs, hyps), refs, hyps


class WERStopCallback(TrainerCallback):
    def __init__(
        self,
        proc,
        subset,
        full,
        every,
        target,
        batch_size,
        max_new_tokens,
        out_dir,
        loss_gate=0.15,
    ):
        self.proc, self.subset, self.full = proc, subset, full
        self.every, self.target, self.bs, self.mnt = (
            every,
            target,
            batch_size,
            max_new_tokens,
        )
        self.out_dir = out_dir
        self.loss_gate = loss_gate

    def _recent_loss(self, state):
        # Mean of the last ~30 logged losses — the single last value is too noisy
        # (per-batch variance 0.1–0.27) and was erratically skipping evals.
        vals = [r["loss"] for r in state.log_history if "loss" in r]
        if not vals:
            return None
        tail = vals[-30:]
        return sum(tail) / len(tail)

    def on_epoch_end(self, args, state, control, model=None, **kwargs):
        ep = int(round(state.epoch))
        if ep == 0 or ep % self.every != 0:
            return control
        # Free-running generation can't be good while teacher-forced loss is high
        # (sample7: WER only collapsed once loss -> ~0). Skip the expensive WER eval
        # until loss drops below the gate, to avoid wasting ~10min/eval early on.
        rl = self._recent_loss(state)
        if rl is not None and rl > self.loss_gate:
            print(
                f"[epoch {ep}] loss={rl:.3f} > {self.loss_gate}; skipping WER eval",
                flush=True,
            )
            return control
        t = time.time()
        wer, _, _ = eval_wer(model, self.proc, self.subset, self.bs, self.mnt)
        print(
            f"[epoch {ep}] subset({len(self.subset)}) corpus WER = {wer*100:.2f}%  ({time.time()-t:.0f}s)",
            flush=True,
        )
        if wer < self.target:
            fw, _, _ = eval_wer(model, self.proc, self.full, self.bs, self.mnt)
            print(
                f"[epoch {ep}] FULL({len(self.full)}) corpus WER = {fw*100:.2f}%",
                flush=True,
            )
            if fw < self.target:
                print(
                    f"[epoch {ep}] TARGET REACHED (<{self.target*100:.0f}% on full). Stopping.",
                    flush=True,
                )
                control.should_training_stop = True
        return control


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemma-4-E2B-it")
    p.add_argument("--parquet_glob", default="/home/coshe-data/data/eval-*.parquet")
    p.add_argument("--cache_path", default="/home/gemma4ft/out/coshe_full_cache.pkl")
    p.add_argument("--output_dir", default="/home/gemma4ft/out/full")
    p.add_argument("--epochs", type=float, default=150.0)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--batch_size", type=int, default=4)
    p.add_argument("--lora_r", type=int, default=256)
    p.add_argument("--lora_alpha", type=int, default=512)
    p.add_argument("--optim", default="adamw_8bit")
    p.add_argument("--include_head", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument(
        "--init_adapter",
        default="",
        help="load this adapter as trainable start (fresh optimizer)",
    )
    p.add_argument("--eval_every", type=int, default=2)
    p.add_argument("--wer_target", type=float, default=0.02)
    p.add_argument("--eval_subset", type=int, default=300)
    p.add_argument("--loss_gate", type=float, default=0.15)
    p.add_argument("--eval_batch_size", type=int, default=24)
    p.add_argument("--eval_max_new_tokens", type=int, default=512)
    return p.parse_args()


def main():
    import os

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
    if args.init_adapter:
        # Load a previously-trained adapter as the starting point but with a FRESH
        # optimizer (so a new/higher LR takes effect) — used to accelerate a run
        # whose loss descent has slowed, without losing learned weights.
        print(f"Loading init adapter (trainable): {args.init_adapter}", flush=True)
        model = PeftModel.from_pretrained(model, args.init_adapter, is_trainable=True)
    else:
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

    ds = CosheParquetDataset(
        args.parquet_glob,
        max_seconds=30.0,
        target_max_chars=0,
        cache_path=args.cache_path,
    )
    items = list(ds)
    subset = items[: args.eval_subset]
    print(f"Dataset: {len(items)} samples; eval subset={len(subset)}", flush=True)
    collator = Gemma4AudioCollator(proc)

    targs = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.batch_size,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        lr_scheduler_type="constant",
        warmup_steps=0,
        bf16=True,
        fp16=False,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=2,
        optim=args.optim,
        report_to=[],
        remove_unused_columns=False,
        dataloader_num_workers=0,
        gradient_checkpointing=False,
    )

    cb = WERStopCallback(
        proc,
        subset,
        items,
        args.eval_every,
        args.wer_target,
        args.eval_batch_size,
        args.eval_max_new_tokens,
        args.output_dir,
        loss_gate=args.loss_gate,
    )
    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=ds,
        data_collator=collator,
        callbacks=[cb],
    )

    resume = (
        args.resume
        and os.path.isdir(args.output_dir)
        and any(d.startswith("checkpoint-") for d in os.listdir(args.output_dir))
    )
    print(f"Starting training... (resume={resume})", flush=True)
    trainer.train(resume_from_checkpoint=resume)
    trainer.save_model(args.output_dir)
    proc.save_pretrained(args.output_dir)
    print("DONE (final adapter saved)", flush=True)


if __name__ == "__main__":
    main()
