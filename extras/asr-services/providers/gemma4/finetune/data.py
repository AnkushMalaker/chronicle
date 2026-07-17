"""Dataset + collator for Gemma 4 audio QLoRA fine-tuning on CoSHE-Eval sample7.

The dataset yields raw {audio: float32 16k mono, target: transcript} items.
The collator does the multimodal preprocessing per batch (the HF gemma recipe
pattern): apply_chat_template(tokenize=False) -> processor(text, audio) ->
build labels.

Label masking (loss only on the transcription):
  * everything in the prompt prefix (user turn incl. expanded audio soft
    tokens + the `<|turn>model\n` generation marker) -> -100
  * pad tokens and any multimodal special tokens -> -100
Only the assistant transcription (+ its closing turn token) is supervised.
"""

import json
import os
import wave
from pathlib import Path

import numpy as np
import torch

DEFAULT_PROMPT = (
    "Transcribe the following speech segment in its original language and identify different speakers. "
    "Follow these specific instructions for formatting the answer:\n"
    "* Label each speaker as Speaker 1, Speaker 2, etc.\n"
    "* Format each turn as 'Speaker N: <text>' on its own line.\n"
    "* Start a new line when the speaker changes.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not "
    "one point seven, and write 3 instead of three.\n"
    "* If the audio is silence or contains no speech, respond with exactly: [NO SPEECH]"
)


def _load_wav_16k_mono(path: str, max_seconds: float) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        audio = audio.reshape(-1, nch).mean(axis=1)
    if sr != 16000:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    if max_seconds:
        audio = audio[: int(16000 * max_seconds)]
    return np.ascontiguousarray(audio, dtype=np.float32)


class CosheSample7Dataset(torch.utils.data.Dataset):
    """Reads coshe-eval/sample7/manifest.json -> {audio, target} items."""

    def __init__(
        self, data_dir: str, max_seconds: float = 30.0, target_max_chars: int = 0
    ):
        d = Path(data_dir)
        manifest = json.loads((d / "manifest.json").read_text())
        self.items = []
        for row in manifest:
            wav = d / "audio" / row["audio_file_name"]
            if not wav.exists():
                continue
            target = row["transcription"].strip()
            # When audio is truncated to the model's 30s window, the back half of
            # the full-clip transcript is ungrounded. Truncating the target to ~the
            # portion the model can actually hear makes the overfit reproducible at
            # generation (CoSHE clips run ~18 chars/sec of speech).
            if target_max_chars:
                target = target[:target_max_chars]
            self.items.append(
                {
                    "audio": _load_wav_16k_mono(str(wav), max_seconds),
                    "target": target,
                    "name": row["audio_file_name"],
                }
            )
        if not self.items:
            raise RuntimeError(f"No samples found under {data_dir}")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]


def _decode_audio_bytes_16k_mono(raw: bytes, max_seconds: float) -> np.ndarray:
    import io

    import soundfile as sf

    audio, sr = sf.read(io.BytesIO(raw), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    if max_seconds:
        audio = audio[: int(16000 * max_seconds)]
    return np.ascontiguousarray(audio, dtype=np.float32)


class CosheParquetDataset(torch.utils.data.Dataset):
    """Full CoSHE-Eval: reads parquet shards (audio as embedded wav bytes).

    Pre-decodes everything into RAM once (≈3.8GB for the full 1985 clips at 30s)
    so multi-epoch overfitting doesn't re-decode each epoch. `limit` caps samples
    for quick recipe checks; `max_seconds` truncates to the model's 30s window.
    """

    def __init__(
        self,
        parquet_glob: str,
        max_seconds: float = 30.0,
        target_max_chars: int = 0,
        limit: int = 0,
        cache_path: str = "",
    ):
        import glob
        import pickle

        import pyarrow.parquet as pq

        # Decoding+resampling all 1985 clips off the HDD is ~40 min; cache the
        # decoded (audio, target) items so repeated training runs load instantly.
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                self.items = pickle.load(f)
            if target_max_chars:
                for it in self.items:
                    it["target"] = it["target"][:target_max_chars]
            if limit:
                self.items = self.items[:limit]
            return

        paths = sorted(glob.glob(parquet_glob))
        if not paths:
            raise RuntimeError(f"No parquet shards match {parquet_glob}")
        self.items = []
        for path in paths:
            t = pq.read_table(
                path, columns=["audio_file_name", "transcription", "audio"]
            )
            names = t.column("audio_file_name").to_pylist()
            trans = t.column("transcription").to_pylist()
            audio_col = t.column("audio").to_pylist()  # list of {bytes, path}
            for name, tr, au in zip(names, trans, audio_col):
                if au is None or au.get("bytes") is None or not tr:
                    continue
                self.items.append(
                    {
                        "audio": _decode_audio_bytes_16k_mono(au["bytes"], max_seconds),
                        "target": tr.strip(),  # full target; truncation applied below
                        "name": name,
                    }
                )
                if limit and len(self.items) >= limit:
                    break
            if limit and len(self.items) >= limit:
                break
        if not self.items:
            raise RuntimeError(f"No samples loaded from {parquet_glob}")
        if cache_path:
            with open(cache_path, "wb") as f:
                pickle.dump(self.items, f)
        if target_max_chars:
            for it in self.items:
                it["target"] = it["target"][:target_max_chars]

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]


class Gemma4AudioCollator:
    def __init__(
        self, processor, prompt: str = DEFAULT_PROMPT, mask_prompt: bool = True
    ):
        self.processor = processor
        self.prompt = prompt
        self.mask_prompt = mask_prompt
        tok = processor.tokenizer
        tok.padding_side = "right"
        self.pad_id = tok.pad_token_id
        # multimodal special tokens to exclude from the loss
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

    def _user_turn(self, audio):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": self.prompt},
                {"type": "audio", "audio": audio},
            ],
        }

    def __call__(self, features):
        audios = [f["audio"] for f in features]
        texts_full = []
        for f in features:
            msgs = [
                self._user_turn(f["audio"]),
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
                    [self._user_turn(f["audio"])],
                    tokenize=False,
                    add_generation_prompt=True,
                )
                plen = self.processor(
                    text=[ptext], audio=[f["audio"]], return_tensors="pt"
                )["input_ids"].shape[1]
                labels[i, :plen] = -100

        batch["labels"] = labels
        return batch
