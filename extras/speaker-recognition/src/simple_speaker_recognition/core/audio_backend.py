"""Audio processing backend using PyAnnote and SpeechBrain."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import torch
from pyannote.audio import Audio, Pipeline
from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding
from pyannote.core import Segment

logger = logging.getLogger(__name__)


class AudioBackend:
    """Wrapper around PyAnnote & SpeechBrain components."""

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
            "pyannote/wespeaker-voxceleb-resnet34-LM", device=device
        )
        self.loader = Audio(sample_rate=16_000, mono="downmix")

    def embed(self, wave: torch.Tensor) -> np.ndarray:  # (1, T)
        with torch.inference_mode():
            emb = self.embedder(wave.to(self.device))
        if isinstance(emb, torch.Tensor):
            emb = emb.cpu().numpy()
        return emb / np.linalg.norm(emb, axis=-1, keepdims=True)

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
    ) -> List[Dict]:
        """
        Async wrapper for diarization with automatic chunking for large files.

        Args:
            path: Path to the audio file
            min_speakers: Minimum number of speakers to detect
            max_speakers: Maximum number of speakers to detect
            collar: Gap duration (seconds) to merge between speaker segments
            min_duration_off: Minimum silence duration (seconds) before treating as segment boundary
            max_duration: Maximum duration (seconds) per PyAnnote call - files longer than this are chunked
            chunk_overlap: Overlap (seconds) between chunks for continuity

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

        all_segments = []
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
                import soundfile as sf

                # Extract tensor data and write as WAV
                audio_tensor = chunk_audio.squeeze().cpu().numpy()
                sf.write(tmp.name, audio_tensor, 16000)
                chunk_path = Path(tmp.name)

            try:
                # Diarize this chunk
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

                # Adjust timestamps to absolute time
                for seg in chunk_segments:
                    seg["start"] += current_start
                    seg["end"] += current_start
                    seg["duration"] = seg["end"] - seg["start"]

                # Only keep segments that start before the overlap cutoff
                cutoff = current_start + chunk_duration
                chunk_segments = [
                    seg for seg in chunk_segments if seg["start"] < cutoff
                ]

                logger.debug(f"Chunk {chunk_num}: found {len(chunk_segments)} segments")
                all_segments.extend(chunk_segments)

            finally:
                chunk_path.unlink(missing_ok=True)

            # Move to next chunk
            current_start += chunk_duration

        logger.info(
            f"Chunked diarization complete: {len(all_segments)} segments before merging"
        )

        # Merge adjacent segments from same speaker
        merged = self._merge_segments(all_segments, max_gap=2.0)
        logger.info(f"After merging: {len(merged)} final segments")

        return merged

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

            seg = Segment(start_clamped, end_clamped)
            wav, _ = self.loader.crop(str(path), seg)
        else:
            wav, _ = self.loader(str(path))
        return wav.unsqueeze(0)  # (1, 1, T)
