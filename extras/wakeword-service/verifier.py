"""Second-stage wake-word verifier — confirms an acoustic arm before dispatch.

The acoustic wake model fires on both true "hey hermes" and a class of acoustic
false positives that score *identically* (~0.99); a score threshold cannot tell
them apart. This small per-deployment classifier (trained by
``training/train_verifier.py`` on the reviewed clip corpus) scores the embedding
window that fired and rejects arms it judges false — an openWakeWord-style custom
verifier on the nanowakeword 96-d embeddings.

Pure numpy at inference: it loads folded logistic-regression weights from a
``.npz`` the trainer produces (feature standardisation folded into ``w``/``b``),
so the detector needs no sklearn. The window/pool helpers live here so the
trainer and the detector share ONE definition and cannot drift.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

# The wake model input is a (16, 96) window: 16 embedding frames of 96 dims.
WINDOW = 16
EMBED_DIM = 96


def pool_features(window: np.ndarray, kind: str) -> np.ndarray:
    """Pool a (16, 96) window into a feature vector. Must match the trainer."""
    win = np.asarray(window, dtype=np.float32)
    if kind == "mean":
        return win.mean(axis=0)
    if kind == "meanstd":
        return np.concatenate([win.mean(axis=0), win.std(axis=0)])
    if kind == "flatten":
        return win.reshape(-1)
    raise ValueError(f"unknown pool kind '{kind}'")


def peak_window(feature_buffer: np.ndarray, wake_session, in_name: str):
    """The highest-scoring contiguous 16-frame window of an embedding buffer.

    This is the window that arms (the wake model's peak), so it is what the
    verifier judges. Buffers shorter than 16 frames are front-padded, mirroring
    the model's left-padded input when fewer than 16 frames exist.

    Returns ``(peak_score, window (16,96))``.
    """
    buf = np.asarray(feature_buffer, dtype=np.float32)
    if buf.shape[0] < WINDOW:
        if buf.shape[0]:
            pad = np.repeat(buf[:1], WINDOW - buf.shape[0], axis=0)
        else:
            pad = np.zeros((WINDOW, EMBED_DIM), dtype=np.float32)
        buf = np.concatenate([pad, buf], axis=0)
    windows = np.stack([buf[i : i + WINDOW] for i in range(buf.shape[0] - WINDOW + 1)])
    scores = wake_session.run(None, {in_name: windows.astype(np.float32)})[0].reshape(
        -1
    )
    j = int(np.argmax(scores))
    return float(scores[j]), windows[j]


class WakeVerifier:
    """Loads a trained verifier and judges arm windows. ``score`` is P(true wake)."""

    def __init__(self, path: str, threshold: float | None = None):
        d = np.load(path, allow_pickle=True)
        self.w = d["w"].astype(np.float32)
        self.b = float(d["b"])
        self.pool = str(d["pool"])
        self.window = int(d["window"]) if "window" in d else WINDOW
        # An explicit env override wins; otherwise use the trained operating point.
        self.threshold = (
            float(threshold) if threshold is not None else float(d["threshold"])
        )
        loo = float(d["loo_auc"]) if "loo_auc" in d else float("nan")
        logger.info(
            f"WakeVerifier loaded: {path} (pool={self.pool}, dim={self.w.shape[0]}, "
            f"threshold={self.threshold:.3f}, LOO-AUC={loo:.4f})"
        )

    def score_window(self, window: np.ndarray) -> float:
        """P(true wake) for a single (16, 96) window."""
        x = pool_features(window, self.pool)
        return float(1.0 / (1.0 + np.exp(-(float(self.w @ x) + self.b))))

    def verify(self, feature_buffer: np.ndarray, wake_session, in_name: str):
        """Judge an arm from the interpreter's embedding buffer at arm time.

        Picks the peak (arming) window from the buffer and scores it.
        Returns ``(passed, prob)`` — ``passed`` is True when the arm is a true wake.
        Fails OPEN (passed=True) on any error so the verifier can never silently
        swallow real detections.
        """
        try:
            _peak, win = peak_window(feature_buffer, wake_session, in_name)
            prob = self.score_window(win)
            return prob >= self.threshold, prob
        except Exception as e:  # noqa: BLE001 - never break detection
            logger.warning(f"verifier error, passing arm through: {e}")
            return True, 1.0


def pool_hubert(emb_seq: np.ndarray, kind: str) -> np.ndarray:
    """Pool a (T, 768) HuBERT embedding sequence into a feature vector. The arm
    window the HuBERT model scores is the rolling 1.5 s buffer (~74 frames). Must
    match ``training/train_hubert_verifier.py``."""
    e = np.asarray(emb_seq, dtype=np.float32)
    if e.ndim == 1:  # already pooled
        return e
    if kind == "mean":
        return e.mean(axis=0)
    if kind == "meanstd":
        return np.concatenate([e.mean(axis=0), e.std(axis=0)])
    raise ValueError(f"unknown pool kind '{kind}'")


class HubertVerifier:
    """Second-stage verifier for HuBERT wake words. Same folded-logreg .npz schema
    as :class:`WakeVerifier`, but scores a pooled **768-d HuBERT embedding** of the
    arm window (the rolling 1.5 s the model just fired on) instead of a 96-d Google
    window — HuBERT words expose no nanowakeword feature buffer. Pure numpy."""

    def __init__(self, path: str, threshold: float | None = None):
        d = np.load(path, allow_pickle=True)
        self.w = d["w"].astype(np.float32)
        self.b = float(d["b"])
        self.pool = str(d["pool"])
        self.threshold = (
            float(threshold) if threshold is not None else float(d["threshold"])
        )
        loo = float(d["loo_auc"]) if "loo_auc" in d else float("nan")
        logger.info(
            f"HubertVerifier loaded: {path} (pool={self.pool}, dim={self.w.shape[0]}, "
            f"threshold={self.threshold:.3f}, LOO-AUC={loo:.4f})"
        )

    def score_embedding(self, emb_seq: np.ndarray) -> float:
        """P(true wake) for the arm-window HuBERT embedding (T, 768)."""
        x = pool_hubert(emb_seq, self.pool)
        return float(1.0 / (1.0 + np.exp(-(float(self.w @ x) + self.b))))

    def verify_embedding(self, emb_seq):
        """Judge an arm from the HuBERT interpreter's arm-window embedding.

        Returns ``(passed, prob)``. Fails OPEN on any error (or missing embedding)
        so the verifier can never silently swallow real detections."""
        try:
            if emb_seq is None:
                return True, 1.0
            prob = self.score_embedding(emb_seq)
            return prob >= self.threshold, prob
        except Exception as e:  # noqa: BLE001 - never break detection
            logger.warning(f"hubert verifier error, passing arm through: {e}")
            return True, 1.0
