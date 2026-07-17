"""Backfill per-clip embeddings (SpeakerAudioSegment rows) from enrollment audio.

Historically the service stored only a single averaged centroid per speaker
(`speakers.embedding_data`); the `speaker_audio_segments.embedding` column was never
populated. The enrollment-health audit (and a future multi-vector gallery) need one
embedding per enrolled clip, so this one-time importer walks the on-disk enrollment
audio, embeds each clip with the wespeaker model, and inserts a SpeakerAudioSegment row.

Idempotent: skips any (speaker_id, filename) that already has a row, so it's safe to
re-run after new enrollments. New enrollments persist their own segments going forward
(see enrollment.py:save_segment_record), so this is only for pre-existing data.

Run inside the speaker-service container:
  podman exec speaker-recognition_speaker-service-gpu_1 \
    python3 /app/scripts/backfill_segment_embeddings.py
"""

import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import torch

# Make the package importable when run as a plain script inside the container.
sys.path.insert(0, "/app/src")

from pyannote.audio import Audio  # noqa: E402
from pyannote.audio.pipelines.speaker_verification import (  # noqa: E402
    PretrainedSpeakerEmbedding,
)

from simple_speaker_recognition.database import get_db_session  # noqa: E402
from simple_speaker_recognition.database.models import (  # noqa: E402
    Speaker,
    SpeakerAudioSegment,
)

DATA_DIR = Path(os.getenv("SPEAKER_DATA_DIR", "/app/data"))
ENROLL_DIR = DATA_DIR / "enrollment_audio"
MIN_SAMPLES = 400  # wespeaker fbank window (25 ms @ 16 kHz)


def main() -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    embedder = PretrainedSpeakerEmbedding(
        "pyannote/wespeaker-voxceleb-resnet34-LM", device=device
    )
    loader = Audio(sample_rate=16000, mono="downmix")

    def embed(path: str):
        wav, _ = loader(path)
        wav = wav.unsqueeze(0)
        if wav.shape[-1] < MIN_SAMPLES:
            wav = torch.nn.functional.pad(wav, (0, MIN_SAMPLES - wav.shape[-1]))
        with torch.inference_mode():
            e = embedder(wav.to(device))
        if isinstance(e, torch.Tensor):
            e = e.cpu().numpy()
        e = np.asarray(e).reshape(-1)
        n = np.linalg.norm(e)
        return (e / n) if np.isfinite(n) and n > 0 else None

    session = get_db_session()
    try:
        speaker_ids = {s.id for s in session.query(Speaker).all()}
        # Existing (speaker_id, filename) pairs for idempotency.
        existing = {
            (row.speaker_id, os.path.basename(row.audio_file_path))
            for row in session.query(SpeakerAudioSegment).all()
        }

        added = skipped = orphan = 0
        for user_dir in sorted(glob.glob(str(ENROLL_DIR / "*"))):
            for sdir in sorted(glob.glob(f"{user_dir}/*")):
                speaker_id = os.path.basename(sdir)
                if speaker_id not in speaker_ids:
                    orphan += 1
                    continue
                for wav in sorted(glob.glob(f"{sdir}/*.wav")):
                    fname = os.path.basename(wav)
                    if (speaker_id, fname) in existing:
                        skipped += 1
                        continue
                    try:
                        dur = float(loader.get_duration(wav))
                        vec = embed(wav)
                    except Exception as e:  # noqa: BLE001
                        print(f"  SKIP {wav}: {e}")
                        continue
                    if vec is None:
                        print(f"  NULL embedding {wav}")
                        continue
                    rel = str(Path(wav).resolve().relative_to(ENROLL_DIR.resolve()))
                    session.add(
                        SpeakerAudioSegment(
                            speaker_id=speaker_id,
                            audio_file_path=rel,
                            original_file_path=fname,
                            start_time=0.0,
                            end_time=dur,
                            duration_seconds=dur,
                            embedding=json.dumps(vec.astype(np.float32).tolist()),
                        )
                    )
                    added += 1
            session.commit()

        session.commit()
        print(
            f"Backfill done: added={added} skipped(existing)={skipped} "
            f"orphan_dirs(no speaker row)={orphan}"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
