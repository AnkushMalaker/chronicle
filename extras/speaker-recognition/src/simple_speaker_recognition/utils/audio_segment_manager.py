"""Audio segment storage and management for speakers."""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


class AudioSegmentManager:
    """Manages audio segment storage for speakers."""

    def __init__(self, base_path: str = "data/audio_segments"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _get_user_path(self, user_id: int) -> Path:
        """Get path for user's audio segments."""
        user_path = self.base_path / str(user_id)
        user_path.mkdir(parents=True, exist_ok=True)
        return user_path

    def _get_speaker_path(self, user_id: int, speaker_id: str) -> Path:
        """Get path for speaker's audio segments."""
        speaker_path = self._get_user_path(user_id) / speaker_id
        speaker_path.mkdir(parents=True, exist_ok=True)
        return speaker_path

    def _get_manifest_path(self, user_id: int, speaker_id: str) -> Path:
        """Get path to speaker's manifest file."""
        return self._get_speaker_path(user_id, speaker_id) / "manifest.json"

    def _load_manifest(self, user_id: int, speaker_id: str) -> Dict[str, Any]:
        """Load speaker's manifest file."""
        manifest_path = self._get_manifest_path(user_id, speaker_id)

        if manifest_path.exists():
            try:
                with open(manifest_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading manifest: {e}")
                return self._create_empty_manifest(speaker_id)
        else:
            return self._create_empty_manifest(speaker_id)

    def _save_manifest(self, user_id: int, speaker_id: str, manifest: Dict[str, Any]):
        """Save speaker's manifest file."""
        manifest_path = self._get_manifest_path(user_id, speaker_id)

        # Update timestamp
        manifest["updated_at"] = datetime.utcnow().isoformat()

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    def _create_empty_manifest(self, speaker_id: str) -> Dict[str, Any]:
        """Create empty manifest structure."""
        return {
            "speaker_id": speaker_id,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "segments": [],
            "total_segments": 0,
            "total_duration": 0.0,
        }

    def _get_next_segment_id(self, manifest: Dict[str, Any]) -> str:
        """Get next available segment ID."""
        if not manifest["segments"]:
            return "001"

        # Find highest segment ID
        max_id = max(int(seg["segment_id"]) for seg in manifest["segments"])
        return f"{max_id + 1:03d}"

    def save_speaker_segment(
        self,
        user_id: int,
        speaker_id: str,
        audio_data: np.ndarray,
        sample_rate: int,
        metadata: Dict[str, Any],
    ) -> str:
        """Save audio segment for speaker.

        Args:
            user_id: User ID
            speaker_id: Speaker ID
            audio_data: Audio numpy array
            sample_rate: Sample rate
            metadata: Segment metadata (original_file, start_time, end_time, quality_score, etc.)

        Returns:
            Path to saved segment file
        """
        # Load manifest
        manifest = self._load_manifest(user_id, speaker_id)

        # Get next segment ID
        segment_id = self._get_next_segment_id(manifest)

        # Save audio file
        speaker_path = self._get_speaker_path(user_id, speaker_id)
        segment_filename = f"segment_{segment_id}.wav"
        segment_path = speaker_path / segment_filename

        sf.write(str(segment_path), audio_data, sample_rate)

        # Calculate duration
        duration = len(audio_data) / sample_rate

        # Create segment entry
        segment_entry = {
            "segment_id": segment_id,
            "filename": segment_filename,
            "original_file": metadata.get("original_file", "unknown"),
            "start_time": metadata.get("start_time", 0.0),
            "end_time": metadata.get("end_time", duration),
            "duration": duration,
            "quality_score": metadata.get("quality_score", 0.0),
            "sample_rate": sample_rate,
            "created_at": datetime.utcnow().isoformat(),
        }

        # Add optional fields
        if "embedding" in metadata:
            segment_entry["embedding"] = metadata["embedding"]
        if "transcription" in metadata:
            segment_entry["transcription"] = metadata["transcription"]

        # Update manifest
        manifest["segments"].append(segment_entry)
        manifest["total_segments"] += 1
        manifest["total_duration"] += duration

        # Save manifest
        self._save_manifest(user_id, speaker_id, manifest)

        logger.info(f"Saved segment {segment_id} for speaker {speaker_id}")

        return str(segment_path)

    def load_speaker_segments(self, user_id: int, speaker_id: str) -> Dict[str, Any]:
        """Load all segments for a speaker.

        Returns:
            Dict containing manifest data and segment info
        """
        manifest = self._load_manifest(user_id, speaker_id)
        return manifest

    def load_segment_audio(
        self, user_id: int, speaker_id: str, segment_id: str
    ) -> tuple[np.ndarray, int]:
        """Load audio data for a specific segment.

        Returns:
            Tuple of (audio_data, sample_rate)
        """
        speaker_path = self._get_speaker_path(user_id, speaker_id)
        segment_path = speaker_path / f"segment_{segment_id}.wav"

        if not segment_path.exists():
            raise FileNotFoundError(
                f"Segment {segment_id} not found for speaker {speaker_id}"
            )

        audio_data, sample_rate = sf.read(str(segment_path))
        return audio_data, sample_rate

    def update_segment_embedding(
        self, user_id: int, speaker_id: str, segment_id: str, embedding: np.ndarray
    ):
        """Update embedding for a specific segment."""
        manifest = self._load_manifest(user_id, speaker_id)

        # Find segment
        for segment in manifest["segments"]:
            if segment["segment_id"] == segment_id:
                # Convert embedding to list for JSON serialization
                segment["embedding"] = embedding.tolist()
                break
        else:
            raise ValueError(f"Segment {segment_id} not found")

        # Save updated manifest
        self._save_manifest(user_id, speaker_id, manifest)

    def delete_segment(self, user_id: int, speaker_id: str, segment_id: str):
        """Delete a specific segment."""
        manifest = self._load_manifest(user_id, speaker_id)
        speaker_path = self._get_speaker_path(user_id, speaker_id)

        # Find and remove segment from manifest
        for i, segment in enumerate(manifest["segments"]):
            if segment["segment_id"] == segment_id:
                # Delete audio file
                segment_path = speaker_path / segment["filename"]
                if segment_path.exists():
                    segment_path.unlink()

                # Update manifest
                manifest["total_duration"] -= segment["duration"]
                manifest["total_segments"] -= 1
                manifest["segments"].pop(i)
                break
        else:
            raise ValueError(f"Segment {segment_id} not found")

        # Save updated manifest
        self._save_manifest(user_id, speaker_id, manifest)

    def delete_speaker_segments(self, user_id: int, speaker_id: str):
        """Delete all segments for a speaker."""
        speaker_path = self._get_speaker_path(user_id, speaker_id)

        if speaker_path.exists():
            shutil.rmtree(speaker_path)
            logger.info(f"Deleted all segments for speaker {speaker_id}")

    def export_speaker_segments(
        self, user_id: int, speaker_id: str, output_dir: str, format: str = "segments"
    ) -> Dict[str, Any]:
        """Export speaker segments.

        Args:
            user_id: User ID
            speaker_id: Speaker ID
            output_dir: Output directory path
            format: 'segments' or 'concatenated'

        Returns:
            Export metadata
        """
        manifest = self._load_manifest(user_id, speaker_id)
        output_path = Path(output_dir) / speaker_id
        output_path.mkdir(parents=True, exist_ok=True)

        export_info = {
            "speaker_id": speaker_id,
            "format": format,
            "segments": [],
            "total_duration": 0.0,
        }

        if format == "segments":
            # Copy individual segments
            for segment in manifest["segments"]:
                src_path = (
                    self._get_speaker_path(user_id, speaker_id) / segment["filename"]
                )
                dst_path = output_path / f"audio{segment['segment_id']}.wav"

                if src_path.exists():
                    shutil.copy2(src_path, dst_path)
                    export_info["segments"].append(
                        {
                            "filename": dst_path.name,
                            "duration": segment["duration"],
                            "transcription": segment.get("transcription", ""),
                        }
                    )
                    export_info["total_duration"] += segment["duration"]

        elif format == "concatenated":
            # Concatenate all segments
            all_audio = []
            sample_rate = None

            for segment in manifest["segments"]:
                audio_data, sr = self.load_segment_audio(
                    user_id, speaker_id, segment["segment_id"]
                )

                if sample_rate is None:
                    sample_rate = sr
                elif sr != sample_rate:
                    # Resample if necessary
                    logger.warning(
                        f"Sample rate mismatch in segment {segment['segment_id']}"
                    )

                all_audio.append(audio_data)

            if all_audio:
                concatenated = np.concatenate(all_audio)
                output_file = output_path / "concatenated.wav"
                sf.write(str(output_file), concatenated, sample_rate)

                export_info["segments"].append(
                    {
                        "filename": "concatenated.wav",
                        "duration": len(concatenated) / sample_rate,
                    }
                )
                export_info["total_duration"] = len(concatenated) / sample_rate

        # Save export metadata
        metadata_path = output_path / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(export_info, f, indent=2)

        return export_info

    def get_all_segment_embeddings(
        self, user_id: int, speaker_id: str
    ) -> List[Dict[str, Any]]:
        """Get all segment embeddings for a speaker.

        Returns:
            List of segment data including embeddings
        """
        manifest = self._load_manifest(user_id, speaker_id)

        segments_with_embeddings = []
        for segment in manifest["segments"]:
            if "embedding" in segment:
                segments_with_embeddings.append(
                    {
                        "segment_id": segment["segment_id"],
                        "embedding": segment["embedding"],
                        "quality_score": segment.get("quality_score", 0.5),
                        "duration": segment["duration"],
                    }
                )

        return segments_with_embeddings
