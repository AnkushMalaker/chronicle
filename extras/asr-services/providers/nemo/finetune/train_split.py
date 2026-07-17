"""Stage 3 generalization for nemotron-3.5-asr-streaming (NeMo RNNT-prompt).

Fine-tune on a 20% split of the CoSHE SEGMENTS (leakage-free by source clip), with
val-loss early stopping, then eval the held-out test split vs base — does FT generalize?
Mirrors the gemma4 20% experiment ([[gemma4_coshe_20pct_generalization]]): very low LR,
EarlyStopping(patience), keep the best-val checkpoint.

Unlike train_full.py (overfit, no val), this sets up validation_data + compute_eval_wer,
saves the best-val .nemo, and stops when val_wer stops improving.

Usage:
    python train_split.py --train s3_train.json --val s3_val.json \
        --lr 1e-5 --batch-size 8 --max-epochs 40 --patience 5 \
        --save-dir /home/ft/out/s3 --mode decoder
"""

import argparse
import time
from pathlib import Path

import lightning.pytorch as pl
import nemo.collections.asr as nemo_asr
import torch
from lightning.pytorch.callbacks import Callback, EarlyStopping
from omegaconf import open_dict


class BestNemoSaver(Callback):
    """Save a .nemo whenever val_wer improves (NeMo .nemo, not a Lightning ckpt)."""

    def __init__(self, path: str, monitor: str = "val_wer"):
        self.path = path
        self.monitor = monitor
        self.best = float("inf")

    def on_validation_end(self, trainer, pl_module):
        v = trainer.callback_metrics.get(self.monitor)
        if v is None:
            return
        v = float(v)
        if v < self.best:
            self.best = v
            pl_module.save_to(self.path)
            print(f"[best] {self.monitor}={v:.4f} -> saved {self.path}", flush=True)


def ds_cfg(model, manifest, batch_size, max_dur, shuffle):
    cfg = model.cfg.train_ds
    with open_dict(cfg):
        cfg.manifest_filepath = manifest
        cfg.is_tarred = False
        cfg.tarred_audio_filepaths = None
        cfg.shard_manifests = False
        cfg.use_lhotse = True
        cfg.shuffle = shuffle
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
    ap.add_argument("--train", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--fused-batch-size", type=int, default=1)
    ap.add_argument("--max-dur", type=float, default=30.0)
    ap.add_argument("--mode", choices=["full", "decoder"], default="decoder")
    ap.add_argument("--max-epochs", type=int, default=40)
    ap.add_argument("--patience", type=int, default=5)
    ap.add_argument("--save-dir", required=True)
    args = ap.parse_args()

    torch.set_float32_matmul_precision("high")
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=args.model)
    model.set_trainer(None)

    # enable validation loss (off by default in the pretrain config)
    with open_dict(model.cfg):
        model.cfg.compute_eval_wer = True
    with open_dict(model.cfg.optim):
        model.cfg.optim.name = "adamw"
        model.cfg.optim.lr = args.lr
        model.cfg.optim.weight_decay = 0.0
        model.cfg.optim.pop("sched", None)

    model.setup_training_data(
        ds_cfg(model, args.train, args.batch_size, args.max_dur, True)
    )
    model.setup_validation_data(
        ds_cfg(model, args.val, args.batch_size, args.max_dur, False)
    )

    if args.fused_batch_size > 0 and hasattr(model.joint, "set_fuse_loss_wer"):
        model.joint.set_fuse_loss_wer(True, loss=model.loss, metric=model.wer)
        model.joint.set_fused_batch_size(args.fused_batch_size)

    if args.mode == "decoder":
        for p in model.encoder.parameters():
            p.requires_grad = False

    tr = sum(p.numel() for p in model.parameters() if p.requires_grad)
    tot = sum(p.numel() for p in model.parameters())
    print(
        f"mode={args.mode}  lr={args.lr}  trainable={tr/1e6:.1f}M/{tot/1e6:.1f}M",
        flush=True,
    )

    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    best_path = str(save_dir / "best.nemo")

    trainer = pl.Trainer(
        max_epochs=args.max_epochs,
        accelerator="gpu",
        devices=1,
        precision="bf16-mixed",
        enable_checkpointing=False,
        logger=False,
        enable_progress_bar=True,
        log_every_n_steps=50,
        num_sanity_val_steps=0,
        check_val_every_n_epoch=1,
        limit_val_batches=120,  # ~960 val segs — fast, stable early-stop signal (full set decode/epoch is slow)
        callbacks=[
            EarlyStopping(
                monitor="val_wer", patience=args.patience, mode="min", verbose=True
            ),
            BestNemoSaver(best_path),
        ],
    )
    model.set_trainer(trainer)

    t0 = time.time()
    trainer.fit(model)
    print(f"train done in {time.time()-t0:.0f}s -> best {best_path}", flush=True)


if __name__ == "__main__":
    main()
