"""Stage 1 overfit smoke test for nemotron-3.5-asr-streaming (NeMo RNNT-prompt).

Goal: prove the hardware + data pipeline + NeMo training loop end-to-end by
memorizing 1-2 CoSHE clips to WER -> 0. Mirrors the gemma4/qwen3 `sample7` stage,
but the mechanics are NeMo-native (Lhotse manifest dataloader + Lightning), not
HF PEFT.

Model: EncDecRNNTBPEModelWithPrompt (NeMo main). 24-layer Conformer encoder
(chunked_limited streaming) -> RNNT decoder+joint (13087 BPE). Per-utterance
language comes from the manifest `target_lang` field via the model's
prompt_dictionary (hi-IN -> 6).

Tuning surface (--mode):
  full      : unfreeze everything (most reliable for a 1-2 clip overfit)
  decoder   : freeze encoder, train decoder + joint (+ prompt) only (lighter)

Trains, then evaluates IN-PROCESS (no save/reload) so a WER->0 result can't be a
load-path artifact -- the exact trap that cost 3 runs on gemma4.

Usage (on the GPU box, NeMo-main venv):
    python train_overfit.py --model nvidia/nemotron-3.5-asr-streaming-0.6b \
        --manifest /home/ft/data/overfit.json --steps 400 --lr 1e-4 --mode full
"""

import argparse
import json
import time

import jiwer
import lightning.pytorch as pl  # NeMo 2.x uses the `lightning` namespace
import nemo.collections.asr as nemo_asr
import torch
from nemo.collections.asr.data.audio_to_text_lhotse_prompt_index import (
    LhotseSpeechToTextBpeDatasetWithPromptIndex,
)
from omegaconf import open_dict


def build_train_cfg(model, manifest: str, batch_size: int, max_dur: float):
    """Clone the model's train_ds cfg and point it at our tiny non-tarred manifest."""
    cfg = model.cfg.train_ds
    with open_dict(cfg):
        cfg.manifest_filepath = manifest
        cfg.is_tarred = False
        cfg.tarred_audio_filepaths = None
        cfg.shard_manifests = False
        cfg.use_lhotse = True
        cfg.shuffle = True
        cfg.batch_size = batch_size
        # drop dynamic bucketing/batch_duration so a handful of clips form fixed batches
        cfg.batch_duration = None
        cfg.bucketing_batch_size = None
        cfg.num_buckets = 0
        cfg.max_duration = max_dur  # CoSHE clips run ~54s; default cap is 20
        cfg.min_duration = 0.1
        cfg.num_workers = 2
        cfg.prompt_field = "target_lang"  # already the default, kept explicit
    return cfg


def apply_freeze(model, mode: str):
    if mode == "full":
        for p in model.parameters():
            p.requires_grad = True
        return
    if mode == "decoder":
        model.encoder.freeze()
        for p in model.encoder.parameters():
            p.requires_grad = False
        for mod in (model.decoder, model.joint):
            for p in mod.parameters():
                p.requires_grad = True
        return
    raise SystemExit(f"unknown --mode {mode}")


def n_trainable(model) -> tuple[int, int]:
    tot = sum(p.numel() for p in model.parameters())
    tr = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return tr, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="nvidia/nemotron-3.5-asr-streaming-0.6b")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument(
        "--fused-batch-size",
        type=int,
        default=1,
        help="RNNT joint fused loss/wer sub-batch; keeps the [B,T,U,V] "
        "joint off-GPU on long CoSHE clips (0 disables fusion)",
    )
    ap.add_argument("--max-dur", type=float, default=65.0)
    ap.add_argument("--mode", choices=["full", "decoder"], default="full")
    ap.add_argument("--target-lang", default="hi-IN")
    ap.add_argument("--save", default=None, help="optional .nemo save path")
    args = ap.parse_args()

    torch.set_float32_matmul_precision("high")
    rows = [json.loads(l) for l in open(args.manifest)]
    print(
        f"Overfit on {len(rows)} clip(s): " f"{[r['audio_file_name'] for r in rows]}",
        flush=True,
    )

    model = nemo_asr.models.ASRModel.from_pretrained(model_name=args.model)
    model.set_trainer(None)

    # constant LR, no scheduler -> pure overfit signal (pretrain default lr=0.5/Noam).
    # NeMo's setup_optimization does `optim['sched']['max_steps']=...` whenever a
    # `sched` key is present, so the key must be REMOVED, not set to None.
    with open_dict(model.cfg.optim):
        model.cfg.optim.name = "adamw"
        model.cfg.optim.lr = args.lr
        model.cfg.optim.weight_decay = 0.0
        model.cfg.optim.pop("sched", None)

    model.setup_training_data(
        build_train_cfg(model, args.manifest, args.batch_size, args.max_dur)
    )

    # RNN-T loss on long CoSHE clips (~59s -> T~737 frames, ~480 BPE tokens) would
    # materialize a [B,T,U,13087] joint (~34 GB) and OOM a 40 GB GPU. Fuse loss+WER
    # into the joint so it's computed in `fused_batch_size` sub-batches without ever
    # holding the full tensor.
    if args.fused_batch_size > 0 and hasattr(model.joint, "set_fuse_loss_wer"):
        model.joint.set_fuse_loss_wer(True, loss=model.loss, metric=model.wer)
        model.joint.set_fused_batch_size(args.fused_batch_size)
        print(
            f"fused RNNT loss/wer enabled, fused_batch_size={args.fused_batch_size}",
            flush=True,
        )

    apply_freeze(model, args.mode)
    tr, tot = n_trainable(model)
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
        log_every_n_steps=10,
        limit_val_batches=0,
        num_sanity_val_steps=0,
    )
    model.set_trainer(trainer)

    t0 = time.time()
    trainer.fit(model)
    print(f"train done in {time.time()-t0:.0f}s", flush=True)

    # ---- in-process eval (no reload) ----
    model.eval()
    wavs = [r["audio_filepath"] for r in rows]
    refs = [r["text"] for r in rows]

    # transcribe() builds cuts with supervision.language=None. The prompt-index
    # dataset's default "unified" mode reads that None language -> "Unknown prompt
    # key: 'None'". Force every eval cut to our training target_lang index so the
    # prompt matches training and no None lookup happens.
    # num_workers=0 keeps the dataset in-process so this class override actually
    # applies (worker subprocesses wouldn't see an in-process monkeypatch).
    LhotseSpeechToTextBpeDatasetWithPromptIndex._get_prompt_index_for_cut = (
        lambda self, cut, _tl=args.target_lang: self._get_prompt_index(_tl)
    )

    with torch.no_grad():
        hyps = model.transcribe(
            wavs,
            batch_size=1,
            target_lang=args.target_lang,
            num_workers=0,
            verbose=False,
        )
    hyps = [h.text if hasattr(h, "text") else str(h) for h in hyps]

    for r, h in zip(rows, hyps):
        w = jiwer.wer(r["text"], h)
        print(f"\n[{r['audio_file_name']}] WER={w*100:.2f}%", flush=True)
        print(f"  REF: {r['text'][:160]}", flush=True)
        print(f"  HYP: {h[:160]}", flush=True)
    corpus = jiwer.wer(refs, hyps)
    print(
        f"\n=== OVERFIT corpus WER = {corpus*100:.2f}%  (target: ~0%) ===", flush=True
    )

    if args.save:
        model.save_to(args.save)
        print(f"saved -> {args.save}", flush=True)


if __name__ == "__main__":
    main()
