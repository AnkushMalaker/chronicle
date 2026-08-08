"""Cheap visual signals decoded straight from the capture chunks.

One ffmpeg pass per chunk decodes every frame to a 64x36 grayscale thumbnail --
about 0.4 s and 130 KB per chunk, so a whole capture day costs roughly half a
minute of CPU and nothing in model tokens. From the thumbnails we get:

* ``motion``  -- mean absolute pixel difference from the previous frame. High
  during gameplay, video playback and scrolling; near zero on a menu, a dialog,
  a result screen or an idle editor.
* ``stillness runs`` -- how long the screen has been visually unchanged, which
  is what distinguishes "a screen is being read" from "a screen is being used".
* ``dhash`` -- a 64-bit perceptual hash for recognising that an earlier visual
  state has returned (the doc's "earlier stable state returned" signal).

These are computed from pixels, so they work on the 36% of frames where Wayland
gives no app or window name at all, and on fullscreen games where OCR is noise.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .spipe import Frame

THUMB_W, THUMB_H = 64, 36


@dataclass
class VisualSignal:
    frame_id: int
    motion: float  # 0..1 mean absolute difference from previous frame in chunk
    stillness: int  # consecutive preceding frames with motion < still_threshold
    dhash: int
    brightness: float
    returned_to: int | None = None  # frame_id of an earlier near-identical state

    def summary(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "motion": round(self.motion, 4),
            "stillness": self.stillness,
            "brightness": round(self.brightness, 3),
            "returned_to": self.returned_to,
        }


def _decode_chunk(path: Path) -> np.ndarray:
    """Every frame of a chunk as a (n, H, W) uint8 array of thumbnails."""
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        f"scale={THUMB_W}:{THUMB_H},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    per = THUMB_W * THUMB_H
    n = len(raw) // per
    return np.frombuffer(raw[: n * per], dtype=np.uint8).reshape(n, THUMB_H, THUMB_W)


def _dhash(thumb: np.ndarray) -> int:
    """64-bit difference hash of an 8x8 reduction."""
    small = thumb[::4, ::8].astype(np.int16)  # 9x8 -> use 8 rows, 8 cols + wrap
    small = small[:8, :8]
    diff = small[:, :-1] > small[:, 1:]
    bits = 0
    for i, bit in enumerate(diff.flatten()):
        if bit:
            bits |= 1 << i
    return bits


def visual_signals(
    frames: list[Frame],
    still_threshold: float = 0.004,
    return_hamming: int = 6,
) -> dict[int, VisualSignal]:
    """Visual signals for frames, grouped by chunk so each file decodes once.

    Frames whose chunk is missing (evicted or snapshot-only capture) are skipped
    rather than guessed at.
    """
    by_chunk: dict[str, list[Frame]] = defaultdict(list)
    for f in frames:
        if f.chunk_path:
            by_chunk[f.chunk_path].append(f)

    out: dict[int, VisualSignal] = {}
    history: list[tuple[int, int]] = []  # (dhash, frame_id) of still frames

    for chunk_path, chunk_frames in by_chunk.items():
        path = Path(chunk_path)
        if not path.exists():
            continue
        try:
            thumbs = _decode_chunk(path)
        except subprocess.CalledProcessError:
            continue
        chunk_frames.sort(key=lambda f: f.offset_index)
        still = 0
        for pos, frame in enumerate(chunk_frames):
            idx = frame.offset_index
            if idx >= len(thumbs):
                continue
            cur = thumbs[idx].astype(np.int16)
            if pos == 0 or chunk_frames[pos - 1].offset_index >= len(thumbs):
                motion = 0.0
            else:
                prev = thumbs[chunk_frames[pos - 1].offset_index].astype(np.int16)
                motion = float(np.abs(cur - prev).mean() / 255.0)
            still = still + 1 if motion < still_threshold else 0
            h = _dhash(thumbs[idx])
            returned = None
            if still >= 1:
                for old_h, old_id in reversed(history[-400:]):
                    if old_id == frame.id:
                        continue
                    if bin(old_h ^ h).count("1") <= return_hamming:
                        returned = old_id
                        break
                history.append((h, frame.id))
            out[frame.id] = VisualSignal(
                frame_id=frame.id,
                motion=motion,
                stillness=still,
                dhash=h,
                brightness=float(cur.mean() / 255.0),
                returned_to=returned,
            )
    return out


def visual_boundaries(
    frames: list[Frame],
    signals: dict[int, VisualSignal] | None = None,
    settle_frames: int = 3,
    busy_motion: float = 0.02,
) -> list[dict]:
    """Points where the screen stopped moving and settled, or started moving again.

    "Interaction stopped and a screen settled" is the domain-blind shape of an
    outcome being displayed: a result screen, a confirmation, a finished build, a
    delivered order. "Motion resumed after a settled screen" is the shape of the
    user acting on it.
    """
    signals = signals or visual_signals(frames)
    ordered = [f for f in frames if f.id in signals]
    events: list[dict] = []
    for i, frame in enumerate(ordered):
        sig = signals[frame.id]
        prev = signals[ordered[i - 1].id] if i else None
        if prev is None:
            continue
        if sig.stillness == settle_frames and prev.motion >= busy_motion:
            events.append(
                {
                    "frame_id": frame.id,
                    "utc": frame.timestamp.isoformat(),
                    "kind": "settled",
                    "prev_motion": round(prev.motion, 4),
                }
            )
        if prev.stillness >= settle_frames and sig.motion >= busy_motion:
            events.append(
                {
                    "frame_id": frame.id,
                    "utc": frame.timestamp.isoformat(),
                    "kind": "resumed",
                    "still_for": prev.stillness,
                }
            )
    return events
