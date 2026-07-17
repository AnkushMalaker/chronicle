"""Dataset over the windowed CoSHE manifest (build_windowed_dataset.py output).

Each row is one honest <=30s window: a sliced 16k-mono WAV + its EXACT ground-truth text
(forced-alignment-derived, no first-30s truncation hack). Yields the same
{audio, target, name} shape the Gemma4AudioCollator expects, so training reuses the
existing collator unchanged.

Plain-transcript targets: CoSHE has no speaker labels, so PLAIN_PROMPT asks only for a
verbatim transcript (digits as digits). Train and eval MUST use the same prompt.
"""

import json
import wave

import numpy as np
import torch

PLAIN_PROMPT = (
    "Transcribe the following speech segment verbatim in its original language "
    "(Hindi-English code-mixed). Write digits as digits (e.g. 3, not three). "
    "Output only the transcript, no speaker labels."
)


def _load_wav_f32(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.ascontiguousarray(
        np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    )


class WindowedManifestDataset(torch.utils.data.Dataset):
    """Loads the windows for one split (train/val). Audio is decoded eagerly into RAM
    (train+val is ~1GB) so multi-epoch training doesn't re-read WAVs."""

    def __init__(self, manifest_path: str, split: str):
        self.items = []
        for line in open(manifest_path):
            r = json.loads(line)
            if r["split"] != split:
                continue
            self.items.append(
                {
                    "audio": _load_wav_f32(r["audio"]),
                    "target": r["text"].strip(),
                    "name": r["name"],
                }
            )
        if not self.items:
            raise RuntimeError(f"no '{split}' rows in {manifest_path}")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]
