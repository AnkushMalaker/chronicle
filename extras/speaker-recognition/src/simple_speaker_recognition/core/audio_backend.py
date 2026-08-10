"""Audio processing backend using PyAnnote and SpeechBrain."""

import asyncio
import io
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import soundfile as sf
import torch
import torchaudio
from pyannote.audio import Audio, Pipeline
from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding
from pyannote.core import Segment
from sklearn.cluster import AgglomerativeClustering

logger = logging.getLogger(__name__)


class AudioBackend:
    """Wrapper around PyAnnote & SpeechBrain components."""

    EMBEDDING_MODEL_ID = "pyannote/wespeaker-voxceleb-resnet34-LM"

    def __init__(self, hf_token: str, device: torch.device):
        self.device = device
        self.diar = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-community-1", token=hf_token
        ).to(device)

        # Configure pipeline with proper segmentation parameters to reduce over-segmentation
        # Note: embedding model is fixed in pre-trained pipeline and cannot be changed at instantiation
        pipeline_params = {
            "segmentation": {
                "min_duration_off": 1.5  # Fill gaps shorter than 1.5 seconds
            }
            # embedding_exclude_overlap is also fixed in the pre-trained pipeline
        }
        self.diar.instantiate(pipeline_params)

        # Use the EXACT same embedding model that the diarization pipeline uses internally
        self.embedder = PretrainedSpeakerEmbedding(
            self.EMBEDDING_MODEL_ID, device=device
        )
        self.loader = Audio(sample_rate=16_000, mono="downmix")

    # wespeaker's fbank front-end uses a 25 ms window (400 samples @ 16 kHz). A waveform
    # shorter than one window makes torchaudio.compliance.kaldi.fbank assert
    # ("choose a window size 400 that is [2, N]") and 500s the whole diarize/identify
    # request — and long multi-speaker recordings routinely yield sub-window diarization
    # fragments. Pad such clips up to one window so embedding degrades gracefully instead
    # of crashing; callers already drop the resulting low-energy/degenerate embeddings.
    MIN_EMBED_SAMPLES = 400
    EMBEDDING_SAMPLE_RATE = 16_000

    def load_wave_bytes(self, content: bytes) -> torch.Tensor:
        """Decode an uploaded audio file directly from memory for identification."""
        if not content:
            raise ValueError("Audio upload is empty")
        try:
            samples, sample_rate = sf.read(
                io.BytesIO(content), dtype="float32", always_2d=True
            )
        except Exception as error:
            raise ValueError("Audio upload is not decodable") from error
        wave = torch.from_numpy(samples.T).mean(dim=0, keepdim=True)
        if sample_rate != self.EMBEDDING_SAMPLE_RATE:
            wave = torchaudio.functional.resample(
                wave, sample_rate, self.EMBEDDING_SAMPLE_RATE
            )
        if wave.shape[-1] == 0:
            raise ValueError("Audio upload contains no samples")
        return wave.unsqueeze(0)

    def embed(self, wave: torch.Tensor) -> np.ndarray:  # (1, T)
        return self.embed_batch([wave], max_batch_size=1)

    def embed_batch(
        self,
        waves: List[torch.Tensor],
        *,
        max_batch_size: int = 32,
        max_padding_ratio: float = 1.25,
    ) -> np.ndarray:
        """Embed many utterances using duration buckets and true model batches.

        The WeSpeaker ONNX export has a batch dimension but no length input. Similar
        durations are therefore grouped and shorter utterances are repeat-padded,
        which preserves their speech distribution better than silence padding.
        Results are returned in the caller's original order.
        """
        if not waves:
            return np.empty((0, self.embedder.dimension), dtype=np.float32)
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")
        if max_padding_ratio < 1.0:
            raise ValueError("max_padding_ratio must be at least 1.0")

        prepared = []
        for index, wave in enumerate(waves):
            if wave.ndim == 3 and wave.shape[0] == 1:
                wave = wave.squeeze(0)
            elif wave.ndim == 1:
                wave = wave.unsqueeze(0)
            if wave.ndim != 2 or wave.shape[0] != 1:
                raise ValueError(
                    f"Expected mono waveform shaped (1, T) or (1, 1, T), got {tuple(wave.shape)}"
                )
            if wave.shape[-1] < self.MIN_EMBED_SAMPLES:
                wave = torch.nn.functional.pad(
                    wave, (0, self.MIN_EMBED_SAMPLES - wave.shape[-1])
                )
            prepared.append((index, wave))

        prepared.sort(key=lambda item: item[1].shape[-1])
        results: List[Optional[np.ndarray]] = [None] * len(prepared)
        cursor = 0
        with torch.inference_mode():
            while cursor < len(prepared):
                shortest = prepared[cursor][1].shape[-1]
                stop = cursor + 1
                while (
                    stop < len(prepared)
                    and stop - cursor < max_batch_size
                    and prepared[stop][1].shape[-1] <= shortest * max_padding_ratio
                ):
                    stop += 1
                bucket = prepared[cursor:stop]
                target_samples = max(wave.shape[-1] for _, wave in bucket)
                padded = []
                for _, wave in bucket:
                    repeats = (target_samples + wave.shape[-1] - 1) // wave.shape[-1]
                    padded.append(wave.repeat(1, repeats)[..., :target_samples])
                model_input = torch.stack(padded).to(self.device)
                embeddings = np.asarray(self.embedder(model_input), dtype=np.float32)
                if embeddings.ndim == 3 and embeddings.shape[1] == 1:
                    embeddings = embeddings[:, 0, :]
                if embeddings.ndim != 2:
                    raise ValueError(
                        "Embedding model returned an unexpected shape: "
                        f"{embeddings.shape}"
                    )
                if embeddings.shape[0] != len(bucket):
                    raise ValueError(
                        "Embedding model returned a different batch size: "
                        f"expected {len(bucket)}, got {embeddings.shape[0]}"
                    )
                norms = np.linalg.norm(embeddings, axis=-1, keepdims=True)
                if not np.all(np.isfinite(norms)) or np.any(norms == 0):
                    raise ValueError(
                        "Embedding model returned a non-finite or zero vector"
                    )
                embeddings = embeddings / norms
                for (original_index, _), embedding in zip(bucket, embeddings):
                    results[original_index] = embedding
                cursor = stop

        if any(result is None for result in results):
            raise RuntimeError("Embedding batch did not produce every requested result")
        return np.stack([result for result in results if result is not None])

    async def async_embed_batch(
        self,
        waves: List[torch.Tensor],
        *,
        max_batch_size: int = 32,
        max_padding_ratio: float = 1.25,
    ) -> np.ndarray:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.embed_batch(
                waves,
                max_batch_size=max_batch_size,
                max_padding_ratio=max_padding_ratio,
            ),
        )

    async def async_embed(self, wave: torch.Tensor) -> np.ndarray:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.embed, wave)

    def diarize(
        self,
        path: Path,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        collar: float = 2.0,
        min_duration_off: float = 1.5,
    ) -> List[Dict]:
        """Perform speaker diarization on an audio file.

        Args:
            path: Path to the audio file
            min_speakers: Minimum number of speakers to detect
            max_speakers: Maximum number of speakers to detect
            collar: Gap duration (seconds) to merge between speaker segments
            min_duration_off: Minimum silence duration (seconds) before treating as segment boundary
        """
        # Dynamically update pipeline parameters if min_duration_off is different from default
        if min_duration_off != 1.5:
            pipeline_params = {"segmentation": {"min_duration_off": min_duration_off}}
            self.diar.instantiate(pipeline_params)

        with torch.inference_mode():
            # Pass speaker count parameters to pyannote
            kwargs = {}
            if min_speakers is not None:
                kwargs["min_speakers"] = min_speakers
            if max_speakers is not None:
                kwargs["max_speakers"] = max_speakers

            output = self.diar(str(path), **kwargs)
            logger.info(f"Diarization output: {output}")

            # In pyannote.audio 4.0+, the pipeline returns a DiarizeOutput object
            # We need to access .speaker_diarization to get the Annotation object
            if hasattr(output, "speaker_diarization"):
                diarization = output.speaker_diarization
                logger.info(f"Using speaker_diarization from output (pyannote 4.0+)")
            else:
                # Fallback for older versions (3.x) that return Annotation directly
                diarization = output
                logger.info(f"Using output directly as Annotation (pyannote 3.x)")

            # Apply PyAnnote's built-in gap filling using support() method with configurable collar
            # This fills gaps shorter than collar seconds between segments from same speaker
            diarization = diarization.support(collar=collar)

        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append(
                {
                    "start": float(turn.start),
                    "end": float(turn.end),
                    "speaker": str(speaker),
                    "duration": float(turn.end - turn.start),
                }
            )

        return segments

    async def async_diarize(
        self,
        path: Path,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        collar: float = 2.0,
        min_duration_off: float = 1.5,
        max_duration: float = 60.0,
        chunk_overlap: float = 5.0,
        reconciliation_threshold: float = 0.4,
    ) -> List[Dict]:
        """
        Async wrapper for diarization with automatic chunking for large files.

        Chunking bounds the cost of pyannote's clustering stage, which runs on CPU
        (scipy ``linkage``) and is O(N^2) in the number of sliding-window embeddings —
        the thing that blows up time/memory on long audio. When a file is chunked,
        each chunk's local ``SPEAKER_xx`` labels are arbitrary, so we DON'T merge by
        label string. Instead we embed each (chunk, local-speaker), then run a second,
        cheap global clustering over those centroids to map them to consistent global
        speaker identities (a.k.a. "speaker linking" / two-pass diarization).

        Args:
            path: Path to the audio file
            min_speakers: Minimum number of speakers to detect
            max_speakers: Maximum number of speakers to detect
            collar: Gap duration (seconds) to merge between speaker segments
            min_duration_off: Minimum silence duration (seconds) before treating as segment boundary
            max_duration: Maximum duration (seconds) per PyAnnote call - files longer than this are chunked
            chunk_overlap: Overlap (seconds) between chunks for continuity
            reconciliation_threshold: Minimum cosine similarity between two chunk-local
                speaker centroids to treat them as the same global speaker

        Returns:
            List of speaker segments (automatically merged if chunked)
        """
        # Get file duration
        file_duration = float(self.loader.get_duration(str(path)))

        # If file is short enough, process in one go
        if file_duration <= max_duration:
            logger.info(
                f"Processing audio without chunking (duration={file_duration:.1f}s ≤ {max_duration}s)"
            )
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self.diarize,
                path,
                min_speakers,
                max_speakers,
                collar,
                min_duration_off,
            )

        # File is too large - chunk it
        logger.info(
            f"Processing audio with chunking (duration={file_duration:.1f}s > {max_duration}s)"
        )
        logger.info(
            f"Using {int(file_duration / max_duration) + 1} chunks with {chunk_overlap}s overlap"
        )

        # Each segment carries a per-chunk-unique tag (f"{chunk}::{local_label}") so
        # that local labels from different chunks never accidentally collide. The tag
        # is replaced by a globally-consistent label during reconciliation below.
        all_segments = []
        centroids: Dict[str, np.ndarray] = {}
        tag_to_chunk: Dict[str, int] = {}
        current_start = 0.0
        chunk_num = 0

        while current_start < file_duration:
            chunk_num += 1
            chunk_duration = min(max_duration, file_duration - current_start)

            # Add overlap for continuity (except for last chunk)
            fetch_duration = (
                chunk_duration + chunk_overlap
                if current_start + chunk_duration < file_duration
                else chunk_duration
            )

            logger.debug(
                f"Processing chunk {chunk_num}: start={current_start:.1f}s, duration={chunk_duration:.1f}s"
            )

            # Load audio segment
            chunk_audio = self.load_wave(
                path, start=current_start, end=current_start + fetch_duration
            )

            # Write chunk to temp file for PyAnnote
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                # Extract tensor data and write as WAV
                audio_tensor = chunk_audio.squeeze().cpu().numpy()
                sf.write(tmp.name, audio_tensor, 16000)
                chunk_path = Path(tmp.name)

            try:
                # Diarize this chunk (segments carry LOCAL, 0-based timestamps)
                loop = asyncio.get_running_loop()
                chunk_segments = await loop.run_in_executor(
                    None,
                    self.diarize,
                    chunk_path,
                    min_speakers,
                    max_speakers,
                    collar,
                    min_duration_off,
                )

                # Only keep segments that start before the overlap cutoff (local time)
                chunk_segments = [
                    seg for seg in chunk_segments if seg["start"] < chunk_duration
                ]

                # Embed each chunk-local speaker BEFORE the temp file is deleted, so we
                # can later link them across chunks by voice rather than by label.
                local_centroids = await self._embed_local_speakers(
                    chunk_path, chunk_segments
                )
                for local_label, centroid in local_centroids.items():
                    tag = f"{chunk_num:03d}::{local_label}"
                    centroids[tag] = centroid
                    tag_to_chunk[tag] = chunk_num

                # Tag segments and shift to absolute time
                for seg in chunk_segments:
                    tag = f"{chunk_num:03d}::{seg['speaker']}"
                    all_segments.append(
                        {
                            "start": seg["start"] + current_start,
                            "end": seg["end"] + current_start,
                            "duration": seg["end"] - seg["start"],
                            "speaker": tag,
                        }
                    )

                logger.debug(f"Chunk {chunk_num}: found {len(chunk_segments)} segments")

            finally:
                chunk_path.unlink(missing_ok=True)

            # Move to next chunk
            current_start += chunk_duration

        logger.info(
            f"Chunked diarization complete: {len(all_segments)} segments "
            f"across {len(centroids)} chunk-local speakers before reconciliation"
        )

        # Map per-chunk tags -> globally-consistent SPEAKER_xx labels by clustering
        # the chunk-local centroids (cheap: a few dozen vectors, no O(N^2) blowup).
        tag_to_global = self._reconcile_chunk_speakers(
            centroids, tag_to_chunk, reconciliation_threshold, max_speakers
        )
        for seg in all_segments:
            seg["speaker"] = tag_to_global.get(seg["speaker"], seg["speaker"])

        n_global = len(set(tag_to_global.values()))
        logger.info(
            f"Reconciled {len(centroids)} chunk-local speakers into {n_global} "
            f"global speakers (threshold={reconciliation_threshold})"
        )

        # Merge adjacent segments from the same (global) speaker
        merged = self._merge_segments(all_segments, max_gap=2.0)
        logger.info(f"After merging: {len(merged)} final segments")

        return merged

    async def _embed_local_speakers(
        self,
        chunk_path: Path,
        segments: List[Dict],
        min_emb_duration: float = 1.0,
        max_pool_duration: float = 12.0,
    ) -> Dict[str, np.ndarray]:
        """Compute one L2-normalized centroid embedding per local speaker in a chunk.

        Pools embeddings from the speaker's longest segments (preferring those >=
        ``min_emb_duration`` since short-utterance embeddings are unreliable; falling
        back to the single longest segment if none qualify, so every local speaker
        still gets a centroid). Embeddings are taken from single-speaker segments only
        — pyannote diarization has already separated overlapping speech into distinct
        labels.
        """
        by_label: Dict[str, List[Dict]] = {}
        for seg in segments:
            by_label.setdefault(seg["speaker"], []).append(seg)

        loop = asyncio.get_running_loop()
        centroids: Dict[str, np.ndarray] = {}
        for label, segs in by_label.items():
            segs_sorted = sorted(
                segs, key=lambda s: s["end"] - s["start"], reverse=True
            )
            chosen = [
                s for s in segs_sorted if (s["end"] - s["start"]) >= min_emb_duration
            ]
            if not chosen:
                chosen = segs_sorted[:1]  # best-effort: longest available segment

            embeddings = []
            pooled = 0.0
            for s in chosen:
                wave = self.load_wave(chunk_path, s["start"], s["end"])
                try:
                    emb = np.asarray(
                        await loop.run_in_executor(None, self.embed, wave)
                    ).reshape(-1)
                except ValueError as error:
                    logger.warning(
                        "Skipping unusable embedding for local speaker %s: %s",
                        label,
                        error,
                    )
                    continue
                # Silent/degenerate segments embed to a zero vector, which self.embed
                # normalizes to NaN (0/0). Drop those — a NaN centroid would crash the
                # downstream clustering (sklearn rejects NaN).
                if np.all(np.isfinite(emb)) and np.linalg.norm(emb) > 0:
                    embeddings.append(emb)
                pooled += s["end"] - s["start"]
                if pooled >= max_pool_duration:
                    break

            if not embeddings:
                logger.warning(
                    f"No finite embedding for local speaker {label} "
                    f"(silent/degenerate segments); skipping reconciliation for it"
                )
                continue

            centroid = np.mean(np.stack(embeddings), axis=0)
            norm = np.linalg.norm(centroid)
            if not np.isfinite(norm) or norm == 0:
                continue
            centroids[label] = centroid / norm
        return centroids

    def _reconcile_chunk_speakers(
        self,
        centroids: Dict[str, np.ndarray],
        tag_to_chunk: Dict[str, int],
        threshold: float,
        max_speakers: Optional[int] = None,
    ) -> Dict[str, str]:
        """Link per-chunk speaker centroids into globally-consistent speaker labels.

        Runs agglomerative clustering over the chunk-local centroids using a precomputed
        cosine-distance matrix with AVERAGE linkage (lenient enough to merge the same
        speaker across noisy short-chunk centroids — complete linkage badly under-merges
        here). Same-chunk speakers get a large sentinel distance to discourage merging
        speakers the diarizer already split. If clustering still leaves more groups than
        ``max_speakers``, force exactly ``max_speakers`` by merging the closest groups —
        a hard upper bound matching the configured speaker count.

        Returns a mapping {chunk_tag -> "SPEAKER_NN"}.
        """
        tags = list(centroids.keys())
        if not tags:
            return {}
        if len(tags) == 1:
            return {tags[0]: "SPEAKER_00"}

        matrix = np.stack([centroids[t] for t in tags])  # (M, D), unit-norm
        distance = 1.0 - (matrix @ matrix.T)  # cosine distance in [0, 2]
        # Safety net: any non-finite distance (degenerate centroid that slipped
        # through) becomes "maximally far" so it never merges, rather than crashing
        # sklearn's clustering, which rejects NaN.
        distance = np.nan_to_num(distance, nan=2.0, posinf=2.0, neginf=0.0)
        np.clip(distance, 0.0, 2.0, out=distance)
        np.fill_diagonal(distance, 0.0)

        # Soft cannot-link: same-chunk speakers get a large sentinel distance so average
        # linkage strongly resists merging speakers the diarizer split within a chunk.
        CANNOT_LINK = 10.0
        for i, ti in enumerate(tags):
            for j, tj in enumerate(tags):
                if i != j and tag_to_chunk[ti] == tag_to_chunk[tj]:
                    distance[i, j] = CANNOT_LINK

        labels = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=1.0 - threshold,
            metric="precomputed",
            linkage="average",
        ).fit_predict(distance)

        # Hard cap: never emit more global speakers than configured max_speakers.
        n_found = len(set(labels))
        if max_speakers and n_found > max_speakers:
            logger.info(
                f"Reconciliation found {n_found} groups > max_speakers={max_speakers}; "
                f"merging closest groups down to {max_speakers}"
            )
            labels = AgglomerativeClustering(
                n_clusters=max_speakers,
                metric="precomputed",
                linkage="average",
            ).fit_predict(distance)

        # Number clusters by first appearance for stable, readable SPEAKER_NN labels.
        order: Dict[int, int] = {}
        mapping: Dict[str, str] = {}
        for tag, raw in zip(tags, labels):
            if raw not in order:
                order[raw] = len(order)
            mapping[tag] = f"SPEAKER_{order[raw]:02d}"
        return mapping

    def _merge_segments(self, segments: List[Dict], max_gap: float = 2.0) -> List[Dict]:
        """Merge adjacent segments from same speaker."""
        if not segments:
            return []

        segments = sorted(segments, key=lambda s: s["start"])
        merged = []
        current = segments[0].copy()

        for next_seg in segments[1:]:
            # Same speaker and close enough?
            if (
                current["speaker"] == next_seg["speaker"]
                and next_seg["start"] - current["end"] <= max_gap
            ):
                # Merge
                current["end"] = next_seg["end"]
                current["duration"] = current["end"] - current["start"]
            else:
                # Save current, start new
                merged.append(current)
                current = next_seg.copy()

        merged.append(current)
        return merged

    def load_wave(
        self, path: Path, start: Optional[float] = None, end: Optional[float] = None
    ) -> torch.Tensor:
        if start is not None and end is not None:
            # Get audio file duration to validate segment bounds
            file_info = self.loader.get_duration(str(path))
            file_duration = float(file_info)

            # Clamp segment bounds to file duration
            start_clamped = max(0.0, min(start, file_duration))
            end_clamped = max(start_clamped, min(end, file_duration))

            # Log if we had to clamp the segment
            if start != start_clamped or end != end_clamped:
                logger.warning(
                    f"Segment [{start:.6f}s, {end:.6f}s] clamped to [{start_clamped:.6f}s, {end_clamped:.6f}s] for file duration {file_duration:.6f}s"
                )

            # mode="pad" zero-pads short reads at file end — duration metadata can
            # over-report by a few ms vs decodable samples, and strict mode raises
            # on the final diarization chunk in that case.
            seg = Segment(start_clamped, end_clamped)
            wav, _ = self.loader.crop(str(path), seg, mode="pad")
        else:
            wav, _ = self.loader(str(path))
        return wav.unsqueeze(0)  # (1, 1, T)
