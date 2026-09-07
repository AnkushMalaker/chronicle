"""HuBERT + conv-attention wake-word backend (GPU), drop-in for ``NanoInterpreter``.

The stock service scores 16x96 Google-embedding windows with a tiny ONNX head
(``NanoInterpreter``). For the hard single word "hermes" those frozen embeddings
cap too low; this backend swaps in a fine-tuned **HuBERT-base + conv-attention head**
(PyTorch, GPU) which separates "hermes" far better (see RESEARCH-single-word-wakeword.md).

It mirrors the exact surface ``detector.py`` uses:
  - ``HubertInterpreter.load_model(path)``  -> instance (HuBERT base shared across
    instances/clients via a class cache; only the rolling audio buffer is per-instance)
  - ``predict(frame_1280) -> {"hubert": score}``   (frame = 1280 int16 samples)
  - ``reset()``                                     (clears the rolling buffer)
  - ``.models = {"hubert": shim}`` with ``get_inputs()[0].name`` so detector's probe
    and (unused) verifier-input lookup don't break.

No ``.preprocessor`` is exposed, so ``detector._snapshot_buffers`` degrades gracefully
(its try/except returns ``(None, b"")``) and HuBERT words simply run stage-1 only
(no nanowakeword 96-d verifier — that operates on Google features, not HuBERT).

Routing: a model file ending in ``.pt`` is loaded by this backend; ``.onnx`` stays
on ``NanoInterpreter``. Selected via ``WAKEWORD_MODELS=hermes:hermes_hubert_convattn.pt``.
"""

from __future__ import annotations

import logging

import numpy as np
import torch
import torch.nn as nn
import torchaudio

logger = logging.getLogger(__name__)

SR = 16000
WLEN = int(SR * 1.5)  # 1.5 s receptive window (matches training)
KEY = "hubert"


class ConvAttn(nn.Module):
    """Same head as training (e7_augment.py): Conv1d + self-attention + mean-pool -> logit."""

    def __init__(
        self, d: int, layer_dim: int = 64, n_blocks: int = 1, n_heads: int = 4
    ):
        super().__init__()
        self.proj = nn.Conv1d(d, layer_dim, 3, padding=1)
        self.convs = nn.ModuleList(
            [nn.Conv1d(layer_dim, layer_dim, 3, padding=1) for _ in range(n_blocks)]
        )
        self.norm1 = nn.LayerNorm(layer_dim)
        while layer_dim % n_heads != 0:
            n_heads -= 1
        self.attn = nn.MultiheadAttention(layer_dim, n_heads, batch_first=True)
        self.attn_norm = nn.LayerNorm(layer_dim)
        self.head = nn.Linear(layer_dim, 1)

    def forward(self, seq: torch.Tensor) -> torch.Tensor:
        h = seq.transpose(1, 2)
        h = torch.relu(self.proj(h))
        for c in self.convs:
            h = torch.relu(c(h))
        h = h.transpose(1, 2)
        h = self.norm1(h)
        a, _ = self.attn(h, h, h, need_weights=False)
        h = self.attn_norm(h + a)
        return self.head(h.mean(dim=1)).squeeze(-1)


class _Inp:
    def __init__(self, name: str):
        self.name = name


class _ModelShim:
    """Stands in for the ONNX session in ``interp.models[key]`` so detector's
    ``get_inputs()[0].name`` probe works without an ONNX model."""

    def get_inputs(self):
        return [_Inp("input")]


class HubertInterpreter:
    # path -> (w2v, head, device); HuBERT base + head loaded once, shared read-only.
    _cache: dict = {}

    def __init__(self, w2v, head, device: str):
        self._w2v = w2v
        self._head = head
        self._dev = device
        self._buf = np.zeros(
            WLEN, dtype=np.float32
        )  # rolling 1.5 s, per-instance/client
        # (T, 768) HuBERT embedding of the buffer scored by the LAST predict() —
        # i.e. the arm window when patience trips. Snapshotted by the second-stage
        # verifier (no second front-end). None until the first predict().
        self._last_emb: np.ndarray | None = None
        self.models = {KEY: _ModelShim()}

    @classmethod
    def load_model(cls, path: str) -> "HubertInterpreter":
        if path not in cls._cache:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            w2v = torchaudio.pipelines.HUBERT_BASE.get_model().to(device).eval()
            head = ConvAttn(768).to(device).eval()
            sd = torch.load(path, map_location=device)
            w2v.load_state_dict(sd["hubert"])
            head.load_state_dict(sd["head"])
            for p in w2v.parameters():
                p.requires_grad_(False)
            cls._cache[path] = (w2v, head, device)
            logger.info(f"HuBERT+conv-attn loaded from {path} on {device}")
        w2v, head, device = cls._cache[path]
        return cls(w2v, head, device)

    def reset(self) -> None:
        self._buf[:] = 0.0
        self._last_emb = None

    def arm_window_embedding(self) -> "np.ndarray | None":
        """The (T, 768) HuBERT embedding of the window the model last scored —
        the arm window when patience trips. Used by :class:`HubertVerifier`."""
        return self._last_emb

    def predict(self, frame) -> dict:
        """Score the rolling 1.5 s window after appending one 1280-sample frame."""
        if isinstance(frame, (bytes, bytearray)):
            x = np.frombuffer(frame, dtype=np.int16)
        else:
            x = np.asarray(frame).reshape(-1)
            if x.dtype != np.int16:
                x = x.astype(np.int16)
        x = x.astype(np.float32) / 32768.0
        n = len(x)
        if n:
            self._buf = np.roll(self._buf, -n)
            self._buf[-n:] = x
        t = torch.from_numpy(self._buf).to(self._dev)[None]
        t = (t - t.mean()) / (t.std() + 1e-6)
        with torch.no_grad():
            feats, _ = self._w2v.extract_features(t)
            last = feats[-1]
            score = torch.sigmoid(self._head(last)).item()
        # Cache the arm-window embedding (T, 768) for the second-stage verifier.
        self._last_emb = last[0].detach().cpu().numpy()
        return {KEY: float(score)}


def is_hubert_model(path: str) -> bool:
    return path.endswith(".pt")
