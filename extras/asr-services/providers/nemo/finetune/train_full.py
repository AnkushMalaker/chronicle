"""Stage 2 full-CoSHE overfit for nemotron-3.5-asr-streaming (NeMo RNNT-prompt).

Memorize all 1985 CoSHE clips to <2% WER (capacity proof), via the warm->anneal
recipe proven on gemma4/qwen3 ([[gemma4_qlora_finetune]], [[qwen3_asr_coshe_overfit]]):

  warm:   --model <hf id>      --lr 2e-4    train until train-loss plateaus
  anneal: --init <warm.nemo>   --lr 3e-5    fresh optimizer -> loss collapses

Full fine-tune (mode=full) already trains the RNNT joint's 13087-way output
projection, so it's the max-capacity setting (no separate "include_head" needed,
unlike the HF decoder-LM models).

Runs full-length on correct full targets (CoSHE has no word timestamps, so audio
must NOT be truncated) — requires A100-80GB: one ~60s clip's RNNT joint is ~34 GB.

Checkpoints every --save-every steps to --save-dir so the run is stop/resume/anneal
-able. WER eval is a SEPARATE step (eval_full.py) — in-training transcribe competes
with the ~34 GB training footprint and risks OOM.

Usage (A100-80GB, NeMo-main venv):
    python train_full.py --manifest /home/ft/data_full/all.json \
        --lr 2e-4 --steps 200000 --batch-size 1 --save-dir /home/ft/out/warm
    python train_full.py --init /home/ft/out/warm/step120000.nemo \
        --manifest /home/ft/data_full/all.json --lr 3e-5 --steps 20000 \
        --save-dir /home/ft/out/anneal
"""

import argparse
import time
from pathlib import Path

import lightning.pytorch as pl
import nemo.collections.asr as nemo_asr
import torch
from lightning.pytorch.callbacks import Callback
from omegaconf import open_dict


class PeriodicSave(Callback):
    """Save a .nemo every `every` global steps (NeMo .nemo, not a Lightning ckpt)."""

    def __init__(self, save_dir: str, every: int):
        self.save_dir = Path(save_dir)
        self.every = every
        self.save_dir.mkdir(parents=True, exist_ok=True)

    def on_train_batch_end(self, trainer, pl_module, *args, **kwargs):
        step = trainer.global_step
        if step > 0 and step % self.every == 0:
            path = self.save_dir / f"step{step}.nemo"
            pl_module.save_to(str(path))
            print(f"[ckpt] step {step} -> {path}", flush=True)


def build_train_cfg(model, manifest: str, batch_size: int, max_dur: float):
    cfg = model.cfg.train_ds
    with open_dict(cfg):
        cfg.manifest_filepath = manifest
        cfg.is_tarred = False
        cfg.tarred_audio_filepaths = None
        cfg.shard_manifests = False
        cfg.use_lhotse = True
        cfg.shuffle = True
        cfg.batch_size = batch_size
        cfg.batch_duration = None
        cfg.bucketing_batch_size = None
        cfg.num_buckets = 0
        cfg.max_duration = max_dur
        cfg.min_duration = 0.1
        cfg.num_workers = 4
        cfg.prompt_field = "target_lang"
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="nvidia/nemotron-3.5-asr-streaming-0.6b")
    ap.add_argument(
        "--init", default=None, help="resume from a saved .nemo (fresh optimizer)"
    )
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--steps", type=int, default=200000)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--fused-batch-size", type=int, default=1)
    ap.add_argument("--max-dur", type=float, default=65.0)
    ap.add_argument("--mode", choices=["full", "decoder"], default="full")
    ap.add_argument("--save-dir", required=True)
    ap.add_argument("--save-every", type=int, default=20000)
    args = ap.parse_args()

    torch.set_float32_matmul_precision("high")

    if args.init:
        print(f"Resuming from {args.init} (fresh optimizer, lr={args.lr})", flush=True)
        model = nemo_asr.models.ASRModel.restore_from(args.init)
    else:
        print(f"Loading base {args.model} (lr={args.lr})", flush=True)
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=args.model)
    model.set_trainer(None)

    with open_dict(model.cfg.optim):
        model.cfg.optim.name = "adamw"
        model.cfg.optim.lr = args.lr
        model.cfg.optim.weight_decay = 0.0
        model.cfg.optim.pop("sched", None)

    model.setup_training_data(
        build_train_cfg(model, args.manifest, args.batch_size, args.max_dur)
    )

    if args.fused_batch_size > 0 and hasattr(model.joint, "set_fuse_loss_wer"):
        model.joint.set_fuse_loss_wer(True, loss=model.loss, metric=model.wer)
        model.joint.set_fused_batch_size(args.fused_batch_size)
        print(
            f"fused RNNT loss/wer enabled, fused_batch_size={args.fused_batch_size}",
            flush=True,
        )

    if args.mode == "decoder":
        for p in model.encoder.parameters():
            p.requires_grad = False

    tr = sum(p.numel() for p in model.parameters() if p.requires_grad)
    tot = sum(p.numel() for p in model.parameters())
    print(
        f"mode={args.mode}  trainable={tr/1e6:.1f}M / {tot/1e6:.1f}M ({100*tr/tot:.1f}%)",
        flush=True,
    )

    trainer = pl.Trainer(
        max_steps=args.steps,
        accelerator="gpu",
        devices=1,
        precision="bf16-mixed",
        enable_checkpointing=False,
        logger=False,
        enable_progress_bar=True,
        log_every_n_steps=50,
        limit_val_batches=0,
        num_sanity_val_steps=0,
        callbacks=[PeriodicSave(args.save_dir, args.save_every)],
    )
    model.set_trainer(trainer)

    t0 = time.time()
    trainer.fit(model)
    final = Path(args.save_dir) / "final.nemo"
    model.save_to(str(final))
    print(f"train done in {time.time()-t0:.0f}s -> {final}", flush=True)


if __name__ == "__main__":
    main()
