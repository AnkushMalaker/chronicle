"""LoRA overfit of Qwen3-ASR on CoSHE sample7, with gemma4's anneal + WER<target early-stop.

Reuses the official ``qwen3_asr_sft.py`` data/forward/label pipeline (prefix-masked target,
audio-path collator, ``patch_outer_forward`` → ``thinker.forward``) but:
  - trains **LoRA on the Qwen3 text decoder only** (``thinker.model.layers.*`` q/k/v/o/gate/up/down),
    AuT audio encoder (``thinker.audio_tower``) frozen — mirrors the gemma4 text-decoder-only recipe;
  - runs `lr_scheduler_type="constant"` and supports a fresh-optimizer **anneal restart** via
    ``--init_adapter`` (warm at high LR, then re-launch from the checkpoint at a lower LR to settle);
  - a loss-gated ``WERStopCallback`` evaluates corpus WER on the 7 clips each ``--eval_every`` epochs
    and stops at ``--wer_target`` (default 2%). transformers 4.57 has no use_cache bug, so eval uses
    normal cached generation via ``wrapper.transcribe``.
"""

import argparse
import time

import jiwer
import torch
from peft import LoraConfig, PeftModel, get_peft_model
from qwen3_asr_sft import (
    DataCollatorForQwen3ASRFinetuning,
    find_latest_checkpoint,
    make_preprocess_fn_prefix_only,
    patch_outer_forward,
)
from transformers import GenerationConfig, Trainer, TrainerCallback, TrainingArguments

# LoRA on the Qwen3 text decoder only (excludes thinker.audio_tower.* and lm_head).
LORA_TARGETS = r".*thinker\.model\.layers\..*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
# With --include_head, also adapt the output head + input embeddings for more memorization
# capacity on the full dataset (gemma4 needed this to drive full-CoSHE WER below 2%).
LORA_TARGETS_HEAD = (
    r"(.*thinker\.model\.layers\..*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
    r"|.*thinker\.model\.embed_tokens|.*thinker\.lm_head)"
)

_ASR_TAG = "<asr_text>"
_WER_NORM = jiwer.Compose(
    [
        jiwer.ToLowerCase(),
        jiwer.RemovePunctuation(),
        jiwer.RemoveMultipleSpaces(),
        jiwer.Strip(),
        jiwer.ReduceToListOfListOfWords(),
    ]
)


def strip_target(text: str) -> str:
    """'language None<asr_text>foo' -> 'foo' (the payload we compare against)."""
    return text.split(_ASR_TAG, 1)[1] if _ASR_TAG in text else text


def corpus_wer(refs, hyps):
    return jiwer.wer(
        refs, hyps, reference_transform=_WER_NORM, hypothesis_transform=_WER_NORM
    )


def char_sim(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


@torch.inference_mode()
def eval_clips(wrapper, items, chunk=4):
    """Transcribe clips (language=None), chunked, and return (wer, exact, refs, hyps)."""
    refs = [strip_target(it["text"]) for it in items]
    hyps = []
    for s in range(0, len(items), chunk):
        paths = [it["audio"] for it in items[s : s + chunk]]
        results = wrapper.transcribe(paths, language=[None] * len(paths))
        hyps.extend(r.text for r in results)
    exact = sum(1 for r, h in zip(refs, hyps) if r.strip() == h.strip())
    return corpus_wer(refs, hyps), exact, refs, hyps


class WERStopCallback(TrainerCallback):
    """Eval corpus WER each `every` epochs (loss-gated). Subset gate -> full confirm (gemma4 style)."""

    def __init__(
        self, wrapper, items, every, target, loss_gate, eval_subset=0, chunk=4
    ):
        self.wrapper, self.items = wrapper, items
        self.every, self.target, self.loss_gate = every, target, loss_gate
        self.chunk = chunk
        self.subset = (
            items[:eval_subset] if eval_subset and eval_subset < len(items) else items
        )

    def _recent_loss(self, state):
        vals = [r["loss"] for r in state.log_history if "loss" in r]
        return sum(vals[-30:]) / len(vals[-30:]) if vals else None

    def on_epoch_end(self, args, state, control, model=None, **kwargs):
        ep = int(round(state.epoch))
        if ep == 0 or ep % self.every != 0:
            return control
        rl = self._recent_loss(state)
        if rl is not None and rl > self.loss_gate:
            print(
                f"[epoch {ep}] loss={rl:.3f} > gate {self.loss_gate}; skip WER eval",
                flush=True,
            )
            return control
        was_training = model.training
        model.eval()
        import gc

        gc.collect()
        torch.cuda.empty_cache()  # free training caches before generation
        t = time.time()
        wer, exact, _, _ = eval_clips(self.wrapper, self.subset, chunk=self.chunk)
        print(
            f"[epoch {ep}] subset({len(self.subset)}) corpus WER = {wer*100:.2f}%  "
            f"exact={exact}/{len(self.subset)}  ({time.time()-t:.0f}s)",
            flush=True,
        )
        if wer < self.target and len(self.subset) < len(self.items):
            fw, fex, _, _ = eval_clips(self.wrapper, self.items, chunk=self.chunk)
            print(
                f"[epoch {ep}] FULL({len(self.items)}) corpus WER = {fw*100:.2f}%  "
                f"exact={fex}/{len(self.items)}",
                flush=True,
            )
            wer = fw
        gc.collect()
        torch.cuda.empty_cache()
        if was_training:
            model.train()
        if wer < self.target:
            print(
                f"[epoch {ep}] TARGET REACHED (corpus WER < {self.target*100:.0f}%). Stopping.",
                flush=True,
            )
            control.should_training_stop = True
        return control


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="Qwen/Qwen3-ASR-0.6B")
    p.add_argument("--train_file", default="/home/qwen3ft/train.jsonl")
    p.add_argument("--output_dir", default="/home/qwen3ft/out/sample7-overfit")
    p.add_argument("--epochs", type=float, default=150.0)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--batch_size", type=int, default=1)
    p.add_argument("--grad_acc", type=int, default=1)
    p.add_argument("--lora_r", type=int, default=16)
    p.add_argument("--lora_alpha", type=int, default=32)
    p.add_argument(
        "--include_head", action="store_true", help="also LoRA lm_head + embed_tokens"
    )
    p.add_argument("--optim", default="adamw_torch")
    p.add_argument(
        "--init_adapter",
        default="",
        help="load this adapter as trainable start (fresh optimizer) for LR anneal",
    )
    p.add_argument("--resume", action="store_true")
    p.add_argument("--eval_every", type=int, default=2)
    p.add_argument(
        "--eval_subset", type=int, default=0, help="WER-gate on first N clips; 0 = all"
    )
    p.add_argument(
        "--eval_chunk",
        type=int,
        default=4,
        help="transcribe batch size during WER eval (small to avoid OOM atop training memory)",
    )
    p.add_argument("--wer_target", type=float, default=0.02)
    p.add_argument("--loss_gate", type=float, default=0.15)
    p.add_argument(
        "--eval_max_new_tokens",
        type=int,
        default=2048,
        help="generation cap for WER eval; 512 truncates long CoSHE transcripts -> false high WER",
    )
    p.add_argument("--sr", type=int, default=16000)
    return p.parse_args()


def main():
    import json
    import os

    args = parse_args()
    torch.manual_seed(0)

    from qwen_asr import Qwen3ASRModel

    wrapper = Qwen3ASRModel.from_pretrained(
        args.model,
        dtype=torch.bfloat16,
        device_map=None,
        max_new_tokens=args.eval_max_new_tokens,
    )
    model = wrapper.model
    processor = wrapper.processor
    patch_outer_forward(model)
    # Qwen3ASRForConditionalGeneration doesn't expose (get|set)_input_embeddings at the top
    # level; PEFT needs them when LoRA targets embed_tokens (--include_head). Delegate to thinker.
    cls = type(model)
    if not getattr(cls, "_emb_patched", False):
        cls.get_input_embeddings = lambda self: self.thinker.model.embed_tokens
        cls.set_input_embeddings = lambda self, v: setattr(
            self.thinker.model, "embed_tokens", v
        )
        cls._emb_patched = True
    model.generation_config = GenerationConfig.from_model_config(model.config)
    model.to("cuda")
    model.config.use_cache = False

    for pm in model.parameters():
        pm.requires_grad = False
    # No gradient checkpointing: a 0.6B + LoRA fits a 40GB A100 with full activations, and
    # Qwen3ASRForConditionalGeneration doesn't expose get_input_embeddings at the top level
    # (so enable_input_require_grads / grad-checkpointing error). LoRA on the decoder layers
    # introduces the grad-requiring path on its own with the base frozen.

    if args.init_adapter:
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

    from datasets import load_dataset

    raw = load_dataset("json", data_files={"train": args.train_file})
    ds = raw.map(make_preprocess_fn_prefix_only(processor), num_proc=1)
    keep = {"prompt", "audio", "target", "prefix_text"}
    drop = [c for c in ds["train"].column_names if c not in keep]
    if drop:
        ds["train"] = ds["train"].remove_columns(drop)
    collator = DataCollatorForQwen3ASRFinetuning(
        processor=processor, sampling_rate=args.sr
    )

    # raw items (audio path + full target) for the WER callback
    with open(args.train_file) as f:
        items = [json.loads(line) for line in f if line.strip()]

    targs = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_acc,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        lr_scheduler_type="constant",
        warmup_steps=0,
        bf16=True,
        fp16=False,
        logging_steps=5,
        save_strategy="epoch",
        save_total_limit=2,
        optim=args.optim,
        report_to="none",
        remove_unused_columns=False,
        dataloader_num_workers=0,
        gradient_checkpointing=False,
    )

    cb = WERStopCallback(
        wrapper,
        items,
        args.eval_every,
        args.wer_target,
        args.loss_gate,
        eval_subset=args.eval_subset,
        chunk=args.eval_chunk,
    )

    class BF16CastTrainer(Trainer):
        def _prepare_inputs(self, inputs):
            inputs = super()._prepare_inputs(inputs)
            for k, v in list(inputs.items()):
                if torch.is_tensor(v) and v.is_floating_point():
                    inputs[k] = v.to(dtype=torch.bfloat16)
            return inputs

    trainer = BF16CastTrainer(
        model=model,
        args=targs,
        train_dataset=ds["train"],
        data_collator=collator,
        tokenizer=processor.tokenizer,
        callbacks=[cb],
    )

    resume = (
        args.resume
        and os.path.isdir(args.output_dir)
        and find_latest_checkpoint(args.output_dir) is not None
    )
    print(f"Starting training... (resume={resume})", flush=True)
    trainer.train(resume_from_checkpoint=resume)
    trainer.save_model(args.output_dir)
    print("DONE (adapter saved)", flush=True)


if __name__ == "__main__":
    main()
