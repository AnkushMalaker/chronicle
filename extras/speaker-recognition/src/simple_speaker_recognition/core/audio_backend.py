"""Audio processing backend using PyAnnote and SpeechBrain."""

import asyncio
import logging
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
            'segmentation': {
                'min_duration_off': 1.5  # Fill gaps shorter than 1.5 seconds
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

    def diarize(self, path: Path, min_speakers: Optional[int] = None, max_speakers: Optional[int] = None, 
                collar: float = 2.0, min_duration_off: float = 1.5) -> List[Dict]:
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
            pipeline_params = {
                'segmentation': {
                    'min_duration_off': min_duration_off
                }
            }
            self.diar.instantiate(pipeline_params)
        
        with torch.inference_mode():
            # Pass speaker count parameters to pyannote
            kwargs = {}
            if min_speakers is not None:
                kwargs['min_speakers'] = min_speakers
            if max_speakers is not None:
                kwargs['max_speakers'] = max_speakers

            output = self.diar(str(path), **kwargs)
            logger.info(f"Diarization output: {output}")

            # In pyannote.audio 4.0+, the pipeline returns a DiarizeOutput object
            # We need to access .speaker_diarization to get the Annotation object
            if hasattr(output, 'speaker_diarization'):
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
            segments.append({
                "start": float(turn.start),
                "end": float(turn.end),
                "speaker": str(speaker),
                "duration": float(turn.end - turn.start)
            })
        
        return segments

    async def async_diarize(self, path: Path, min_speakers: Optional[int] = None, max_speakers: Optional[int] = None,
                           collar: float = 2.0, min_duration_off: float = 1.5) -> List[Dict]:
        """Async wrapper for diarization.
        
        Args:
            path: Path to the audio file
            min_speakers: Minimum number of speakers to detect
            max_speakers: Maximum number of speakers to detect
            collar: Gap duration (seconds) to merge between speaker segments
            min_duration_off: Minimum silence duration (seconds) before treating as segment boundary
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.diarize, path, min_speakers, max_speakers, collar, min_duration_off)

    def load_wave(self, path: Path, start: Optional[float] = None, end: Optional[float] = None) -> torch.Tensor:
        if start is not None and end is not None:
            # Get audio file duration to validate segment bounds
            file_info = self.loader.get_duration(str(path))
            file_duration = float(file_info)
            
            # Clamp segment bounds to file duration
            start_clamped = max(0.0, min(start, file_duration))
            end_clamped = max(start_clamped, min(end, file_duration))
            
            # Log if we had to clamp the segment
            if start != start_clamped or end != end_clamped:
                logger.warning(f"Segment [{start:.6f}s, {end:.6f}s] clamped to [{start_clamped:.6f}s, {end_clamped:.6f}s] for file duration {file_duration:.6f}s")
            
            seg = Segment(start_clamped, end_clamped)
            wav, _ = self.loader.crop(str(path), seg)
        else:
            wav, _ = self.loader(str(path))
        return wav.unsqueeze(0)  # (1, 1, T)