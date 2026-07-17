"""Interleaved multi-chunk dataset + collator for the audio->formatted(pasted_text) task.

Each example is ONE clip: its audio is fed as N <=28s chunks (multiple audio blocks in a
single user turn), the target is the WHOLE pasted_text, and the prompt is conditioned on the
app the dictation was going into (Wispr formats per-app). No text windowing — formatted text
isn't word-alignable, so the model attends to all chunks and emits the full formatted clip.

Loss is on the target only (prompt prefix incl. all audio soft-tokens + pad + mm-special
tokens masked to -100), matching the single-audio Gemma4AudioCollator.
"""

import json
import wave

import numpy as np
import torch


def _load_wav_f32(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.ascontiguousarray(
        np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    )


def build_prompt(app: str) -> str:
    """App-aware formatting instruction. Wispr formats dictation differently per target app."""
    where = f"the {app} app" if app else "the target app"
    return (
        f"You are formatting voice dictation that will be typed into {where}. "
        "The audio is provided as one or more consecutive segments of a single dictation. "
        "Transcribe all of it and format the result the way it should appear when typed into "
        f"{where} (punctuation, capitalization, digits as digits, app-appropriate style). "
        "Output only the final formatted text, with no commentary."
    )


class ChunkInterleaveDataset(torch.utils.data.Dataset):
    """One item per clip: {chunks:[float32 16k], target:str, app:str, clip:str}."""

    def __init__(self, manifest_path: str, split: str):
        self.items = []
        for line in open(manifest_path):
            r = json.loads(line)
            if r["split"] != split:
                continue
            self.items.append(
                {
                    "chunks": [_load_wav_f32(p) for p in r["chunks"]],
                    "target": r["target"].strip(),
                    "app": r.get("app", ""),
                    "clip": r["clip"],
                }
            )
        if not self.items:
            raise RuntimeError(f"no '{split}' rows in {manifest_path}")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]


class InterleaveCollator:
    """Batches variable-#chunk examples. Uses the batch processor with audio as a per-example
    list of arrays (nested); the chat template emits one audio placeholder per chunk."""

    def __init__(self, processor, mask_prompt: bool = True):
        self.processor = processor
        self.mask_prompt = mask_prompt
        tok = processor.tokenizer
        tok.padding_side = "right"
        self.pad_id = tok.pad_token_id
        self.special_ids = [
            t
            for t in [
                getattr(tok, "audio_token_id", None),
                getattr(tok, "image_token_id", None),
                getattr(tok, "boi_token_id", None),
                getattr(tok, "eoi_token_id", None),
                getattr(tok, "boa_token_id", None),
                getattr(tok, "eoa_token_id", None),
            ]
            if t is not None
        ]

    def _user_turn(self, chunks, app):
        content = [{"type": "text", "text": build_prompt(app)}]
        content += [{"type": "audio", "audio": a} for a in chunks]
        return {"role": "user", "content": content}

    def __call__(self, features):
        # processor wants a FLAT list of all audios across the batch, matched to the audio
        # placeholders (one per chunk) in text order.
        audios = [a for f in features for a in f["chunks"]]
        texts_full = []
        for f in features:
            msgs = [
                self._user_turn(f["chunks"], f["app"]),
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f["target"]}],
                },
            ]
            texts_full.append(
                self.processor.apply_chat_template(
                    msgs, tokenize=False, add_generation_prompt=False
                )
            )
        batch = self.processor(
            text=texts_full, audio=audios, return_tensors="pt", padding=True
        )

        labels = batch["input_ids"].clone()
        labels[labels == self.pad_id] = -100
        for tid in self.special_ids:
            labels[labels == tid] = -100
        if self.mask_prompt:
            for i, f in enumerate(features):
                ptext = self.processor.apply_chat_template(
                    [self._user_turn(f["chunks"], f["app"])],
                    tokenize=False,
                    add_generation_prompt=True,
                )
                plen = self.processor(
                    text=[ptext], audio=f["chunks"], return_tensors="pt"
                )["input_ids"].shape[1]
                labels[i, :plen] = -100
        batch["labels"] = labels
        return batch
