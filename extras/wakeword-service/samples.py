"""On-disk store for wake-word data-collection clips — the training flywheel.

Every acoustic arm snapshots its trigger window to ``pending/`` for review; a
reviewer labels each true-wake / not-wake, which moves it to ``positive/`` /
``negative/``. The "prime + say it" flow also lands in ``pending/`` (tagged
``false_negative`` when the live model under-scored the utterance), so the user
confirms wake / not-wake before it rolls into training.

The store is **wake-word-scoped**: clips live under a per-wake-word directory so
two wake words running in parallel (e.g. ``hey_hermes`` + ``hermes``) never mix.
A clip is stored under exactly ONE wake word — the word it was enrolled for
(prime) or the word that armed (live capture). When a live arm co-fires several
models, only the arming word's queue gets the clip; the rest are recorded in its
``also_fired`` metadata, never cross-written.

Layout (under WAKEWORD_DATA_DIR, default ``/app/data/samples``)::

    <wakeword>/pending/   <clip>.wav + <clip>.json   awaiting review
    <wakeword>/positive/  <clip>.wav + <clip>.json   confirmed wake word
    <wakeword>/negative/  <clip>.wav + <clip>.json   confirmed NOT the wake word

``positive/`` and ``negative/`` are exactly the dirs the training ingest
(``training/ingest_samples.py --wakeword <w>``) consumes, so a labeled clip is
immediately retrain-ready — no separate export step.

A clip is a 16 kHz mono WAV plus a sidecar JSON of metadata. Labeling/deleting is
a file move/unlink, so the store has no central index to keep consistent.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
import wave
from collections import defaultdict
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

PENDING = "pending"
POSITIVE = "positive"
NEGATIVE = "negative"
BUCKETS = (PENDING, POSITIVE, NEGATIVE)

# A label maps to the bucket a reviewed clip lands in.
_LABEL_BUCKET = {"wake": POSITIVE, "not_wake": NEGATIVE}

_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(name: str) -> str:
    """Filesystem-safe wake-word directory name."""
    return _SANITIZE.sub("", name) or "unknown"


class SampleStore:
    """File-backed store of wake-word clips, bucketed by wake word and review state."""

    def __init__(
        self,
        base_dir: str,
        wakewords: list[str],
        legacy_wakeword: Optional[str] = None,
    ):
        """Create the store, ensuring per-wake-word bucket dirs exist.

        Args:
            base_dir: Root directory for the wake-word sample tree.
            wakewords: The wake words this deployment runs (one subtree each).
            legacy_wakeword: If set and the OLD flat ``base_dir/<bucket>`` layout
                is found (pre-multi-wake-word), its clips are moved one-time into
                ``base_dir/<legacy_wakeword>/<bucket>``. All historically-collected
                clips are "hey hermes", so this keeps them from contaminating a
                second wake word's data.
        """
        self.base_dir = base_dir
        self.wakewords = [_safe(w) for w in wakewords]
        os.makedirs(base_dir, exist_ok=True)
        if legacy_wakeword:
            self._migrate_legacy_flat(_safe(legacy_wakeword))
        for wakeword in self.wakewords:
            for bucket in BUCKETS:
                os.makedirs(os.path.join(base_dir, wakeword, bucket), exist_ok=True)
        logger.info(
            f"SampleStore at {base_dir} "
            f"(wakewords={', '.join(self.wakewords)}; buckets={', '.join(BUCKETS)})"
        )

    def _migrate_legacy_flat(self, target: str) -> None:
        """One-time move of pre-multi-wake-word flat buckets into ``target``.

        The old store wrote ``base_dir/pending|positive|negative`` directly. If
        those exist, relocate their contents under ``base_dir/<target>/<bucket>``
        and remove the now-empty flat dirs. Idempotent — a no-op once migrated.
        """
        for bucket in BUCKETS:
            flat = os.path.join(self.base_dir, bucket)
            if not os.path.isdir(flat):
                continue
            dest = os.path.join(self.base_dir, target, bucket)
            os.makedirs(dest, exist_ok=True)
            moved = 0
            for name in os.listdir(flat):
                os.replace(os.path.join(flat, name), os.path.join(dest, name))
                moved += 1
            try:
                os.rmdir(flat)
            except OSError:
                pass
            if moved:
                logger.info(
                    f"Migrated {moved} legacy '{bucket}' clips -> {target}/{bucket}"
                )

    def _iter_wakewords(self) -> list[str]:
        """Wake-word subtrees currently on disk (configured ones + any extras)."""
        found = set(self.wakewords)
        try:
            for name in os.listdir(self.base_dir):
                if os.path.isdir(os.path.join(self.base_dir, name)):
                    found.add(name)
        except OSError:
            pass
        return sorted(found)

    def _bucket_dir(self, wakeword: str, bucket: str) -> str:
        if bucket not in BUCKETS:
            raise ValueError(f"unknown bucket '{bucket}' (expected {BUCKETS})")
        return os.path.join(self.base_dir, _safe(wakeword), bucket)

    @staticmethod
    def _new_clip_id(client_id: str, created_at_ms: int) -> str:
        client = _SANITIZE.sub("", client_id) or "unknown"
        return f"{created_at_ms}_{client}_{uuid.uuid4().hex[:6]}"

    def _wav_path(self, wakeword: str, bucket: str, clip_id: str) -> str:
        return os.path.join(self._bucket_dir(wakeword, bucket), f"{clip_id}.wav")

    def _json_path(self, wakeword: str, bucket: str, clip_id: str) -> str:
        return os.path.join(self._bucket_dir(wakeword, bucket), f"{clip_id}.json")

    # Dev buffer-state sidecars (interpreter snapshot at arm). Optional — only
    # present for clips captured with WAKEWORD_SAVE_BUFFER_STATE enabled.
    def _features_path(self, wakeword: str, bucket: str, clip_id: str) -> str:
        return os.path.join(
            self._bucket_dir(wakeword, bucket), f"{clip_id}.features.npy"
        )

    def _context_path(self, wakeword: str, bucket: str, clip_id: str) -> str:
        return os.path.join(
            self._bucket_dir(wakeword, bucket), f"{clip_id}.context.wav"
        )

    def _sidecars(self, wakeword: str, bucket: str, clip_id: str) -> tuple:
        """All optional auxiliary files for a clip (buffer-state sidecars)."""
        return (
            self._features_path(wakeword, bucket, clip_id),
            self._context_path(wakeword, bucket, clip_id),
        )

    @staticmethod
    def _write_wav(path: str, pcm: bytes, sample_rate: int) -> None:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)

    @staticmethod
    def _audio_sha1(pcm: bytes) -> str:
        """Content hash of the raw PCM — the dedup key (exact-duplicate clips)."""
        return hashlib.sha1(pcm).hexdigest()

    @staticmethod
    def _read_wav_frames(path: str) -> bytes:
        with wave.open(path, "rb") as wf:
            return wf.readframes(wf.getnframes())

    def _clip_hash(self, wakeword: str, bucket: str, clip_id: str) -> Optional[str]:
        """Content hash for a stored clip — from its JSON if present, else the WAV."""
        try:
            with open(self._json_path(wakeword, bucket, clip_id)) as fh:
                stored = json.load(fh).get("audio_sha1")
            if stored:
                return stored
        except (OSError, json.JSONDecodeError):
            pass
        try:
            return self._audio_sha1(
                self._read_wav_frames(self._wav_path(wakeword, bucket, clip_id))
            )
        except (OSError, wave.Error):
            return None

    def existing_hashes(self, wakeword: str) -> set:
        """Set of content hashes already stored for ``wakeword`` (all buckets).

        Used by the offline farmer to skip clips it already has. Backfills from
        the WAV for older clips that predate the stored ``audio_sha1``.
        """
        out: set = set()
        for bucket in BUCKETS:
            for rec in self.list(wakeword, bucket):
                h = rec.get("audio_sha1") or self._clip_hash(
                    wakeword, bucket, rec["id"]
                )
                if h:
                    out.add(h)
        return out

    def save(
        self,
        wakeword: str,
        bucket: str,
        pcm: bytes,
        sample_rate: int,
        created_at_ms: int,
        meta: dict,
        features: Optional[np.ndarray] = None,
        context_pcm: Optional[bytes] = None,
    ) -> dict:
        """Write a clip (WAV + JSON sidecar) into ``wakeword/bucket``.

        Args:
            wakeword: The wake word this clip belongs to.
            bucket: One of ``pending`` / ``positive`` / ``negative``.
            pcm: Raw int16 mono PCM.
            sample_rate: Sample rate of ``pcm`` (Hz).
            created_at_ms: Capture timestamp (epoch ms) — also seeds the clip id.
            meta: Extra metadata (client_id, score, reason, source, also_fired...).
            features: Optional ``(N, 96)`` interpreter embedding buffer at arm —
                feeding its last 16 frames to the wake model reproduces the live
                score exactly. Saved as ``<id>.features.npy``.
            context_pcm: Optional full ~10 s raw-audio buffer (int16 PCM) at arm,
                the complete context behind the trigger. Saved as
                ``<id>.context.wav``.

        Returns:
            The stored record dict (meta + id/wakeword/bucket/sample_rate/...).
        """
        client_id = str(meta.get("client_id", "unknown"))
        clip_id = self._new_clip_id(client_id, created_at_ms)
        record = {
            **meta,
            "id": clip_id,
            "wakeword": _safe(wakeword),
            "bucket": bucket,
            "sample_rate": sample_rate,
            "created_at_ms": created_at_ms,
            "duration_secs": round(len(pcm) / 2 / max(sample_rate, 1), 2),
            "audio_sha1": self._audio_sha1(pcm),
        }
        self._write_wav(self._wav_path(wakeword, bucket, clip_id), pcm, sample_rate)
        # Optional dev buffer-state sidecars (exact offline reproduction).
        if features is not None:
            np.save(
                self._features_path(wakeword, bucket, clip_id), np.asarray(features)
            )
            record["feature_frames"] = int(np.asarray(features).shape[0])
            record["has_buffer_state"] = True
        if context_pcm:
            self._write_wav(
                self._context_path(wakeword, bucket, clip_id), context_pcm, sample_rate
            )
            record["context_secs"] = round(
                len(context_pcm) / 2 / max(sample_rate, 1), 2
            )
        with open(self._json_path(wakeword, bucket, clip_id), "w") as fh:
            json.dump(record, fh)
        return record

    def list(self, wakeword: str, bucket: str) -> list[dict]:
        """Return all clip records in ``wakeword/bucket``, newest first."""
        d = self._bucket_dir(wakeword, bucket)
        records: list[dict] = []
        if not os.path.isdir(d):
            return records
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name)) as fh:
                    records.append(json.load(fh))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"skipping unreadable sample meta {name}: {e}")
        records.sort(key=lambda r: r.get("created_at_ms", 0), reverse=True)
        return records

    def _find(self, clip_id: str) -> Optional[tuple]:
        """Return ``(wakeword, bucket)`` a clip currently lives in, or None."""
        for wakeword in self._iter_wakewords():
            for bucket in BUCKETS:
                if os.path.exists(self._wav_path(wakeword, bucket, clip_id)):
                    return wakeword, bucket
        return None

    def wav_path(self, clip_id: str) -> Optional[str]:
        """Absolute path to a clip's WAV, wherever it currently lives."""
        loc = self._find(clip_id)
        return self._wav_path(loc[0], loc[1], clip_id) if loc else None

    def label(self, clip_id: str, label: str) -> dict:
        """Apply a review label, moving the clip into its target bucket.

        Args:
            clip_id: Clip identifier.
            label: ``wake`` (-> positive) or ``not_wake`` (-> negative).

        Returns:
            The updated record dict.
        """
        target = _LABEL_BUCKET.get(label)
        if target is None:
            raise ValueError(
                f"unknown label '{label}' (expected {list(_LABEL_BUCKET)})"
            )
        loc = self._find(clip_id)
        if loc is None:
            raise KeyError(clip_id)
        wakeword, src = loc

        with open(self._json_path(wakeword, src, clip_id)) as fh:
            record = json.load(fh)
        record["bucket"] = target
        record["label"] = label

        if src != target:
            os.replace(
                self._wav_path(wakeword, src, clip_id),
                self._wav_path(wakeword, target, clip_id),
            )
            os.replace(
                self._json_path(wakeword, src, clip_id),
                self._json_path(wakeword, target, clip_id),
            )
            # Move buffer-state sidecars alongside, if present.
            for src_aux, dst_aux in zip(
                self._sidecars(wakeword, src, clip_id),
                self._sidecars(wakeword, target, clip_id),
            ):
                if os.path.exists(src_aux):
                    os.replace(src_aux, dst_aux)
        with open(self._json_path(wakeword, target, clip_id), "w") as fh:
            json.dump(record, fh)
        return record

    def move(self, clip_id: str, wakeword: str, bucket: str = PENDING) -> dict:
        """Move a clip to a DIFFERENT wake word's bucket (default pending).

        For the overlap case: a live arm attributed to one word (e.g. ``hey_hermes``
        by priority) that is really an utterance of another (``hermes``). Moves the
        WAV + JSON + sidecars, rewrites ``wakeword``/``bucket``, drops any stale
        ``label`` so it re-enters review, and records ``moved_from``. The embedding
        sidecar is model-agnostic (shared front-end), so it stays valid.
        """
        if bucket not in BUCKETS:
            raise ValueError(f"unknown bucket '{bucket}' (expected {BUCKETS})")
        loc = self._find(clip_id)
        if loc is None:
            raise KeyError(clip_id)
        src_word, src_bucket = loc
        dst_word = _safe(wakeword)
        if dst_word == src_word and bucket == src_bucket:
            return json.load(open(self._json_path(src_word, src_bucket, clip_id)))
        os.makedirs(self._bucket_dir(dst_word, bucket), exist_ok=True)

        with open(self._json_path(src_word, src_bucket, clip_id)) as fh:
            record = json.load(fh)
        record["wakeword"] = dst_word
        record["bucket"] = bucket
        record["moved_from"] = {"wakeword": src_word, "bucket": src_bucket}
        record.pop("label", None)

        os.replace(
            self._wav_path(src_word, src_bucket, clip_id),
            self._wav_path(dst_word, bucket, clip_id),
        )
        for src_aux, dst_aux in zip(
            self._sidecars(src_word, src_bucket, clip_id),
            self._sidecars(dst_word, bucket, clip_id),
        ):
            if os.path.exists(src_aux):
                os.replace(src_aux, dst_aux)
        # Write the updated JSON at the destination, remove the source JSON.
        os.remove(self._json_path(src_word, src_bucket, clip_id))
        with open(self._json_path(dst_word, bucket, clip_id), "w") as fh:
            json.dump(record, fh)
        return record

    def copy(self, clip_id: str, wakeword: str, bucket: str = PENDING) -> dict:
        """Copy a clip into ANOTHER wake word's bucket (source stays put).

        For the shared-false-positive case: one firing that tripped several wake
        words is a hard negative for each of them. Move re-homes (positive belongs
        to one word); copy fans out (the FP belongs to all that fired). The copy
        gets a FRESH clip id so it coexists with the original (per-word dedup won't
        touch cross-word copies). The embedding sidecar is model-agnostic, so it
        copies over valid.
        """
        if bucket not in BUCKETS:
            raise ValueError(f"unknown bucket '{bucket}' (expected {BUCKETS})")
        loc = self._find(clip_id)
        if loc is None:
            raise KeyError(clip_id)
        src_word, src_bucket = loc
        dst_word = _safe(wakeword)
        os.makedirs(self._bucket_dir(dst_word, bucket), exist_ok=True)

        with open(self._json_path(src_word, src_bucket, clip_id)) as fh:
            record = json.load(fh)
        new_id = self._new_clip_id(
            str(record.get("client_id", "unknown")),
            int(record.get("created_at_ms", 0)),
        )
        record["id"] = new_id
        record["wakeword"] = dst_word
        record["bucket"] = bucket
        record["copied_from"] = {
            "id": clip_id,
            "wakeword": src_word,
            "bucket": src_bucket,
        }
        record.pop("label", None)

        shutil.copy2(
            self._wav_path(src_word, src_bucket, clip_id),
            self._wav_path(dst_word, bucket, new_id),
        )
        for src_aux, dst_aux in zip(
            self._sidecars(src_word, src_bucket, clip_id),
            self._sidecars(dst_word, bucket, new_id),
        ):
            if os.path.exists(src_aux):
                shutil.copy2(src_aux, dst_aux)
        with open(self._json_path(dst_word, bucket, new_id), "w") as fh:
            json.dump(record, fh)
        return record

    def delete(self, clip_id: str) -> bool:
        """Remove a clip (WAV + JSON) wherever it lives. Returns True if found."""
        loc = self._find(clip_id)
        if loc is None:
            return False
        wakeword, bucket = loc
        for path in (
            self._wav_path(wakeword, bucket, clip_id),
            self._json_path(wakeword, bucket, clip_id),
            *self._sidecars(wakeword, bucket, clip_id),
        ):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return True

    def stats(self) -> dict:
        """Per-wake-word clip counts (+ how many positives were rescued FNs).

        Returns a dict keyed by wake word::

            {"hey_hermes": {"pending": 3, "positive": 86, "negative": 128,
                            "false_negatives": 4}, ...}
        """
        out: dict[str, dict] = {}
        for wakeword in self._iter_wakewords():
            counts = {}
            for bucket in BUCKETS:
                d = self._bucket_dir(wakeword, bucket)
                # Count the per-clip .json sidecar, NOT .wav: buffer-state capture
                # writes a second `<id>.context.wav` per clip, which would otherwise
                # double the count here vs. what list() (json-based) actually shows.
                counts[bucket] = (
                    sum(1 for n in os.listdir(d) if n.endswith(".json"))
                    if os.path.isdir(d)
                    else 0
                )
            counts["false_negatives"] = sum(
                1 for r in self.list(wakeword, POSITIVE) if r.get("false_negative")
            )
            out[wakeword] = counts
        return out

    # Keep priority when deduping: a labeled clip beats a pending one (don't throw
    # away review effort); among equals, keep the oldest (first collected).
    _DEDUP_RANK = {POSITIVE: 2, NEGATIVE: 2, PENDING: 0}

    def dedupe(self, wakeword: Optional[str] = None) -> dict:
        """Remove exact-duplicate clips (same audio content) within each wake word.

        Groups every clip (pending + positive + negative) by its PCM content hash;
        for each group of duplicates keeps a single representative — preferring an
        already-labeled clip over a pending one (so review effort is never lost),
        and the oldest among equals — and deletes the rest (including duplicate
        pending clips). Returns a per-word summary.

        Args:
            wakeword: Limit to one wake word, or None for all.
        """
        words = [_safe(wakeword)] if wakeword else self._iter_wakewords()
        out: dict = {}
        for w in words:
            groups: dict = defaultdict(list)  # sha1 -> [(bucket, clip_id, created_at)]
            for bucket in BUCKETS:
                for rec in self.list(w, bucket):
                    h = rec.get("audio_sha1") or self._clip_hash(w, bucket, rec["id"])
                    if h:
                        groups[h].append(
                            (bucket, rec["id"], rec.get("created_at_ms", 0))
                        )

            removed: list = []
            conflicts = 0
            for members in groups.values():
                if len(members) < 2:
                    continue
                labeled = {b for b, _, _ in members if b in (POSITIVE, NEGATIVE)}
                if len(labeled) > 1:
                    conflicts += 1  # same audio labeled both wake AND not_wake
                # Keep highest-rank bucket, oldest first; delete the rest.
                ordered = sorted(members, key=lambda m: (-self._DEDUP_RANK[m[0]], m[2]))
                for bucket, clip_id, _ in ordered[1:]:
                    if self.delete(clip_id):
                        removed.append({"id": clip_id, "bucket": bucket})
            out[w] = {
                "duplicate_groups": sum(1 for m in groups.values() if len(m) > 1),
                "removed": len(removed),
                "removed_by_bucket": {
                    b: sum(1 for r in removed if r["bucket"] == b) for b in BUCKETS
                },
                "kept_unique": len(groups),
                "conflicts": conflicts,
            }
            logger.info(
                f"dedupe '{w}': removed {len(removed)} dup(s) "
                f"({out[w]['removed_by_bucket']}), {len(groups)} unique remain"
                + (f", {conflicts} LABEL CONFLICTS" if conflicts else "")
            )
        return out
