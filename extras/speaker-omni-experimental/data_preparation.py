#!/usr/bin/env python3
"""
Data Preparation Script for Qwen2.5-Omni Speaker Recognition

Downloads YouTube audio, transcribes with Deepgram diarization, and organizes
speaker segments for few-shot learning with the Qwen2.5-Omni system.

Usage:
    uv run python data_preparation.py process --url "youtube_url" --output-dir data/
    uv run python data_preparation.py extract-refs --transcript data/transcripts/video.json
    uv run python data_preparation.py generate-config --data-dir data/ --output config.yaml
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import soundfile as sf
import yaml
import yt_dlp
from deepgram import DeepgramClient, PrerecordedOptions
from pydub import AudioSegment
from scipy import signal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("data_preparation.log"), logging.StreamHandler()],
)


class SimpleCache:
    """Simple file-based cache for Deepgram responses."""

    def __init__(self, cache_dir: str = "cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

    def get_file_hash(self, file_path: str) -> str:
        """Get MD5 hash of file for cache key."""
        with open(file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def get_cache_path(self, audio_path: str, params: Dict) -> Path:
        """Generate cache file path."""
        file_hash = self.get_file_hash(audio_path)
        params_hash = hashlib.md5(
            json.dumps(params, sort_keys=True).encode()
        ).hexdigest()
        return self.cache_dir / f"{file_hash}_{params_hash}.json"

    def get(self, audio_path: str, params: Dict) -> Optional[Dict]:
        """Get cached response."""
        cache_path = self.get_cache_path(audio_path, params)
        if cache_path.exists():
            with open(cache_path, "r") as f:
                return json.load(f)
        return None

    def set(self, audio_path: str, params: Dict, response: Any) -> str:
        """Cache response."""
        cache_path = self.get_cache_path(audio_path, params)

        # Convert response to dict
        if hasattr(response, "to_dict"):
            response_dict = response.to_dict()
        elif hasattr(response, "__dict__"):
            response_dict = response.__dict__
        else:
            response_dict = dict(response)

        with open(cache_path, "w") as f:
            json.dump(response_dict, f, indent=2)

        return str(cache_path)


class AudioQualityAssessor:
    """Assess audio quality for reference clips."""

    @staticmethod
    def analyze_segment(audio_data: np.ndarray, sample_rate: int) -> Dict[str, float]:
        """
        Analyze audio segment quality.

        Args:
            audio_data: Audio samples
            sample_rate: Sample rate

        Returns:
            Quality metrics dictionary
        """
        # Calculate RMS energy
        rms = np.sqrt(np.mean(audio_data**2))

        # Calculate zero crossing rate (speech indicator)
        zero_crossings = np.sum(np.diff(np.signbit(audio_data)))
        zcr = zero_crossings / len(audio_data)

        # Calculate spectral centroid (brightness)
        freqs = np.fft.fftfreq(len(audio_data), 1 / sample_rate)
        magnitude = np.abs(np.fft.fft(audio_data))
        spectral_centroid = np.sum(freqs * magnitude) / np.sum(magnitude)

        # Calculate signal-to-noise ratio estimate
        # Use simple energy-based approach
        energy_threshold = np.percentile(audio_data**2, 90)
        signal_samples = audio_data[audio_data**2 > energy_threshold]
        noise_samples = audio_data[audio_data**2 <= energy_threshold]

        signal_power = np.mean(signal_samples**2) if len(signal_samples) > 0 else 0
        noise_power = np.mean(noise_samples**2) if len(noise_samples) > 0 else 1e-10
        snr = 10 * np.log10(signal_power / noise_power) if signal_power > 0 else -100

        return {
            "rms_energy": float(rms),
            "zero_crossing_rate": float(zcr),
            "spectral_centroid": float(abs(spectral_centroid)),
            "snr_estimate": float(snr),
            "duration": len(audio_data) / sample_rate,
        }

    @classmethod
    def is_good_quality(
        cls,
        metrics: Dict[str, float],
        min_duration: float = 5.0,
        max_duration: float = 15.0,
    ) -> bool:
        """
        Determine if segment is good quality for reference.

        Args:
            metrics: Quality metrics from analyze_segment
            min_duration: Minimum duration in seconds
            max_duration: Maximum duration in seconds

        Returns:
            True if good quality
        """
        duration_ok = min_duration <= metrics["duration"] <= max_duration
        energy_ok = metrics["rms_energy"] > 0.01  # Not too quiet
        speech_like = 0.02 < metrics["zero_crossing_rate"] < 0.3  # Speech-like ZCR
        snr_ok = metrics["snr_estimate"] > 5.0  # At least 5dB SNR

        return duration_ok and energy_ok and speech_like and snr_ok


class DataPreparer:
    """Main class for preparing YouTube data for Qwen2.5-Omni training."""

    def __init__(self, deepgram_api_key: str, output_dir: str = "data"):
        """
        Initialize data preparer.

        Args:
            deepgram_api_key: Deepgram API key
            output_dir: Output directory for processed data
        """
        self.deepgram_api_key = deepgram_api_key
        self.deepgram = DeepgramClient(deepgram_api_key)
        self.output_dir = Path(output_dir)
        self.logger = logging.getLogger(__name__)
        self.cache = SimpleCache()
        self.quality_assessor = AudioQualityAssessor()

        # Create directory structure
        self.setup_directories()

    def setup_directories(self):
        """Create output directory structure."""
        directories = [
            self.output_dir,
            self.output_dir / "raw_audio",
            self.output_dir / "segments",
            self.output_dir / "reference_clips",
            self.output_dir / "transcripts",
            self.output_dir / "metadata",
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def sanitize_filename(self, title: str) -> str:
        """Sanitize YouTube title for filename."""
        sanitized = re.sub(r'[<>:"/\\\\|?*]', "", title)
        sanitized = re.sub(r"\\s+", "-", sanitized.strip())
        return sanitized[:50]

    def download_audio(self, url: str) -> Tuple[str, str, Dict]:
        """
        Download audio from YouTube URL.

        Args:
            url: YouTube URL

        Returns:
            Tuple of (audio_path, title, metadata)
        """
        try:
            self.logger.info(f"Starting audio download from: {url}")

            # First get video info
            ydl_opts_info = {"quiet": True}
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                info = ydl.extract_info(url, download=False)
                title = self.sanitize_filename(info["title"])
                duration = info.get("duration", 0)

                metadata = {
                    "original_title": info["title"],
                    "sanitized_title": title,
                    "duration": duration,
                    "url": url,
                    "uploader": info.get("uploader", "Unknown"),
                    "upload_date": info.get("upload_date", "Unknown"),
                }

            self.logger.info(f"Video: {info['title']} ({duration}s)")

            # Download with high quality audio
            output_path = self.output_dir / "raw_audio" / f"{title}.wav"

            if output_path.exists():
                self.logger.info(f"Audio already exists: {output_path}")
                return str(output_path), title, metadata

            ydl_opts = {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "wav",
                        "preferredquality": "192",
                    }
                ],
                # High quality audio settings
                "postprocessor_args": [
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-acodec",
                    "pcm_s16le",
                ],
                "outtmpl": str(output_path.with_suffix("")),
                "quiet": True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if not output_path.exists():
                raise FileNotFoundError(f"Download failed: {output_path}")

            # Convert to mono 16kHz for processing
            processed_path = output_path.with_name(f"{title}_processed.wav")
            audio = AudioSegment.from_wav(str(output_path))
            audio = audio.set_channels(1).set_frame_rate(16000)
            audio.export(str(processed_path), format="wav")

            self.logger.info(f"Audio downloaded and processed: {processed_path}")
            return str(processed_path), title, metadata

        except Exception as e:
            self.logger.error(f"Download failed: {e}")
            raise

    async def transcribe_audio(self, audio_path: str) -> Dict:
        """
        Transcribe audio using Deepgram with speaker diarization.

        Args:
            audio_path: Path to audio file

        Returns:
            Deepgram response dictionary
        """
        try:
            if not Path(audio_path).exists():
                raise FileNotFoundError(f"Audio file not found: {audio_path}")

            # Define API parameters for caching
            api_params = {
                "model": "nova-3",
                "diarize": True,
                "multichannel": False,
                "smart_format": True,
                "punctuate": True,
                "language": "en",
                "utterances": True,
                "paragraphs": True,
            }

            # Check cache first
            cached_response = self.cache.get(audio_path, api_params)
            if cached_response:
                self.logger.info(f"✅ Using cached transcription for {audio_path}")
                return cached_response

            self.logger.info(f"💸 Transcribing with Deepgram: {audio_path}")

            # Read audio file
            with open(audio_path, "rb") as audio_file:
                buffer_data = audio_file.read()

            payload = {"buffer": buffer_data}
            options = PrerecordedOptions(**api_params)

            # Transcribe
            response = await self.deepgram.listen.asyncprerecorded.v(
                "1"
            ).transcribe_file(payload, options)

            if response and hasattr(response, "results"):
                # Cache the response
                cache_key = self.cache.set(audio_path, api_params, response)
                self.logger.info(f"💾 Cached response: {cache_key}")

                # Convert to dict for return
                if hasattr(response, "to_dict"):
                    return response.to_dict()
                else:
                    return response.__dict__
            else:
                raise ValueError("Invalid response from Deepgram")

        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            raise

    def extract_speaker_segments(
        self, transcript_data: Dict, audio_path: str, title: str
    ) -> List[Dict]:
        """
        Extract speaker segments from Deepgram transcript.

        Args:
            transcript_data: Deepgram transcript response
            audio_path: Path to original audio file
            title: Video title for naming

        Returns:
            List of speaker segment dictionaries
        """
        try:
            if (
                "results" not in transcript_data
                or "utterances" not in transcript_data["results"]
            ):
                self.logger.warning("No utterances found in transcript")
                return []

            utterances = transcript_data["results"]["utterances"]
            self.logger.info(f"Found {len(utterances)} utterances")

            # Load audio for segmentation
            audio_data, sample_rate = sf.read(audio_path)

            segments = []
            speaker_stats = {}

            for i, utterance in enumerate(utterances):
                speaker_id = utterance.get("speaker", 0)
                start_time = utterance.get("start", 0)
                end_time = utterance.get("end", 0)
                text = utterance.get("transcript", "")
                confidence = utterance.get("confidence", 0)

                # Skip very short or low confidence segments
                duration = end_time - start_time
                if duration < 2.0 or confidence < 0.7:
                    continue

                # Extract audio segment
                start_sample = int(start_time * sample_rate)
                end_sample = int(end_time * sample_rate)
                segment_audio = audio_data[start_sample:end_sample]

                # Assess quality
                quality_metrics = self.quality_assessor.analyze_segment(
                    segment_audio, sample_rate
                )

                segment_info = {
                    "segment_id": f"{title}_segment_{i:03d}",
                    "speaker_id": f"speaker_{speaker_id}",
                    "start_time": start_time,
                    "end_time": end_time,
                    "duration": duration,
                    "text": text,
                    "confidence": confidence,
                    "quality_metrics": quality_metrics,
                    "is_good_quality": self.quality_assessor.is_good_quality(
                        quality_metrics
                    ),
                }

                # Save segment audio
                segment_filename = f"{segment_info['segment_id']}.wav"
                segment_path = self.output_dir / "segments" / segment_filename
                sf.write(str(segment_path), segment_audio, sample_rate)
                segment_info["audio_path"] = str(segment_path)

                segments.append(segment_info)

                # Update speaker statistics
                if speaker_id not in speaker_stats:
                    speaker_stats[speaker_id] = {
                        "total_duration": 0,
                        "segment_count": 0,
                        "good_quality_count": 0,
                        "avg_confidence": 0,
                    }

                stats = speaker_stats[speaker_id]
                stats["total_duration"] += duration
                stats["segment_count"] += 1
                stats["avg_confidence"] = (
                    stats["avg_confidence"] * (stats["segment_count"] - 1) + confidence
                ) / stats["segment_count"]
                if segment_info["is_good_quality"]:
                    stats["good_quality_count"] += 1

            # Save segment metadata
            metadata = {
                "segments": segments,
                "speaker_statistics": speaker_stats,
                "total_segments": len(segments),
                "audio_file": audio_path,
                "title": title,
            }

            metadata_path = self.output_dir / "metadata" / f"{title}_segments.json"
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

            self.logger.info(
                f"Extracted {len(segments)} segments from {len(speaker_stats)} speakers"
            )
            return segments

        except Exception as e:
            self.logger.error(f"Segment extraction failed: {e}")
            raise

    def organize_reference_clips(
        self, segments: List[Dict], min_clips_per_speaker: int = 3
    ) -> Dict[str, List[str]]:
        """
        Organize best segments as reference clips for each speaker.

        Args:
            segments: List of segment dictionaries
            min_clips_per_speaker: Minimum clips needed per speaker

        Returns:
            Dictionary mapping speaker_id to list of reference clip paths
        """
        try:
            # Group segments by speaker
            speaker_segments = {}
            for segment in segments:
                speaker_id = segment["speaker_id"]
                if speaker_id not in speaker_segments:
                    speaker_segments[speaker_id] = []
                speaker_segments[speaker_id].append(segment)

            reference_clips = {}

            for speaker_id, speaker_segs in speaker_segments.items():
                # Filter for good quality segments
                good_segments = [s for s in speaker_segs if s["is_good_quality"]]

                if len(good_segments) < min_clips_per_speaker:
                    self.logger.warning(
                        f"Only {len(good_segments)} good quality segments for {speaker_id}"
                    )
                    # Use best available segments even if not perfect quality
                    good_segments = sorted(
                        speaker_segs, key=lambda x: x["confidence"], reverse=True
                    )

                # Sort by quality score (combination of confidence and SNR)
                def quality_score(seg):
                    confidence = seg["confidence"]
                    snr = seg["quality_metrics"].get("snr_estimate", 0)
                    duration_bonus = 1.0 if 5 <= seg["duration"] <= 15 else 0.8
                    return confidence * 0.5 + (snr / 20) * 0.3 + duration_bonus * 0.2

                good_segments.sort(key=quality_score, reverse=True)

                # Select top segments as reference clips
                selected_segments = good_segments[
                    : min_clips_per_speaker * 2
                ]  # Get extra for variety

                # Create speaker directory
                speaker_dir = self.output_dir / "reference_clips" / speaker_id
                speaker_dir.mkdir(exist_ok=True)

                reference_paths = []
                for i, segment in enumerate(selected_segments):
                    # Copy segment to reference clips directory
                    ref_filename = f"{speaker_id}_ref_{i+1:02d}.wav"
                    ref_path = speaker_dir / ref_filename
                    shutil.copy2(segment["audio_path"], ref_path)
                    reference_paths.append(str(ref_path))

                reference_clips[speaker_id] = reference_paths
                self.logger.info(
                    f"Created {len(reference_paths)} reference clips for {speaker_id}"
                )

            return reference_clips

        except Exception as e:
            self.logger.error(f"Reference clip organization failed: {e}")
            raise

    def generate_config_yaml(
        self, reference_clips: Dict[str, List[str]], output_path: str
    ):
        """
        Generate config.yaml for Qwen2.5-Omni system.

        Args:
            reference_clips: Dictionary mapping speaker_id to reference clip paths
            output_path: Path to save config.yaml
        """
        try:
            # Convert speaker_0, speaker_1 to human-readable names
            speaker_mapping = {}
            for speaker_id in reference_clips.keys():
                # Extract speaker number
                speaker_num = speaker_id.split("_")[-1]
                # Generate placeholder name
                placeholder_names = ["Alice", "Bob", "Charlie", "Diana", "Eve", "Frank"]
                if speaker_num.isdigit() and int(speaker_num) < len(placeholder_names):
                    human_name = placeholder_names[int(speaker_num)]
                else:
                    human_name = f"Person_{speaker_num}"
                speaker_mapping[speaker_id] = human_name

            # Build config structure
            config = {
                "model": {"model_id": "Qwen/Qwen2.5-Omni-7B", "device_map": "auto"},
                "audio": {
                    "chunk_duration": 30,
                    "overlap_duration": 5,
                    "auto_chunk": True,
                },
                "speakers": {},
                "output": {
                    "format": "json",
                    "include_metadata": True,
                    "save_segments": False,
                },
                "advanced": {
                    "max_new_tokens": 1024,
                    "temperature": 0.0,
                    "enable_audio_output": False,
                },
            }

            # Add speakers with human names
            for speaker_id, ref_paths in reference_clips.items():
                human_name = speaker_mapping[speaker_id]
                # Convert absolute paths to relative paths from config location
                config_dir = Path(output_path).parent
                relative_paths = []
                for path in ref_paths:
                    try:
                        rel_path = Path(path).relative_to(config_dir)
                        relative_paths.append(str(rel_path))
                    except ValueError:
                        # If relative path fails, use absolute path
                        relative_paths.append(path)

                config["speakers"][human_name] = relative_paths

            # Add comments as a separate section for user guidance
            config["_instructions"] = {
                "speaker_names": "Replace Alice, Bob, etc. with actual names of family members",
                "reference_clips": "Each speaker needs 2-3 clean audio clips of 5-15 seconds",
                "model_selection": "Use Qwen/Qwen2.5-Omni-3B for faster processing on limited VRAM",
                "generated_from": "Auto-generated from YouTube video analysis",
            }

            # Save config
            with open(output_path, "w") as f:
                yaml.dump(
                    config, f, default_flow_style=False, sort_keys=False, indent=2
                )

            self.logger.info(f"Generated config.yaml: {output_path}")
            self.logger.info(
                f"Found {len(reference_clips)} speakers: {list(speaker_mapping.values())}"
            )

        except Exception as e:
            self.logger.error(f"Config generation failed: {e}")
            raise

    async def process_youtube_url(self, url: str) -> Tuple[str, Dict[str, List[str]]]:
        """
        Complete processing pipeline for a YouTube URL.

        Args:
            url: YouTube URL

        Returns:
            Tuple of (title, reference_clips_dict)
        """
        try:
            # Download audio
            audio_path, title, metadata = self.download_audio(url)

            # Save metadata
            metadata_path = self.output_dir / "metadata" / f"{title}_download.json"
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

            # Transcribe
            transcript_data = await self.transcribe_audio(audio_path)

            # Save transcript
            transcript_path = (
                self.output_dir / "transcripts" / f"{title}_transcript.json"
            )
            with open(transcript_path, "w") as f:
                json.dump(transcript_data, f, indent=2)

            # Extract segments
            segments = self.extract_speaker_segments(transcript_data, audio_path, title)

            # Organize reference clips
            reference_clips = self.organize_reference_clips(segments)

            # Generate config
            config_path = self.output_dir / f"{title}_config.yaml"
            self.generate_config_yaml(reference_clips, str(config_path))

            self.logger.info(f"✅ Processing complete for: {title}")
            return title, reference_clips

        except Exception as e:
            self.logger.error(f"Processing failed: {e}")
            raise


def main():
    """CLI interface."""
    parser = argparse.ArgumentParser(
        description="Data preparation for Qwen2.5-Omni speaker recognition"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Process command
    process_parser = subparsers.add_parser("process", help="Process YouTube URL")
    process_parser.add_argument("--url", required=True, help="YouTube URL")
    process_parser.add_argument("--output-dir", default="data", help="Output directory")
    process_parser.add_argument(
        "--deepgram-key", help="Deepgram API key (or set DEEPGRAM_API_KEY env var)"
    )

    # Extract references command
    extract_parser = subparsers.add_parser(
        "extract-refs", help="Extract reference clips from existing transcript"
    )
    extract_parser.add_argument(
        "--transcript", required=True, help="Path to transcript JSON file"
    )
    extract_parser.add_argument(
        "--audio", required=True, help="Path to corresponding audio file"
    )
    extract_parser.add_argument("--output-dir", default="data", help="Output directory")

    # Generate config command
    config_parser = subparsers.add_parser(
        "generate-config", help="Generate config.yaml from reference clips"
    )
    config_parser.add_argument(
        "--data-dir", required=True, help="Data directory containing reference_clips/"
    )
    config_parser.add_argument(
        "--output", default="config.yaml", help="Output config file"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Get Deepgram API key
    deepgram_key = args.deepgram_key or os.getenv("DEEPGRAM_API_KEY")
    if not deepgram_key and args.command in ["process"]:
        print(
            "Error: Deepgram API key required. Set DEEPGRAM_API_KEY env var or use --deepgram-key"
        )
        return

    if args.command == "process":
        # Process YouTube URL
        preparer = DataPreparer(deepgram_key, args.output_dir)

        async def run_processing():
            title, reference_clips = await preparer.process_youtube_url(args.url)
            print(f"\\n✅ Processing complete!")
            print(f"Title: {title}")
            print(f"Speakers found: {len(reference_clips)}")
            print(f"Output directory: {args.output_dir}")
            print(f"Config file: {args.output_dir}/{title}_config.yaml")
            print("\\nNext steps:")
            print("1. Review reference clips in reference_clips/ directory")
            print("2. Edit speaker names in the generated config.yaml")
            print(
                "3. Test with: uv run python qwen_speaker_diarizer.py transcribe --config config.yaml --audio test.wav"
            )

        asyncio.run(run_processing())

    elif args.command == "extract-refs":
        # Extract references from existing transcript
        preparer = DataPreparer("dummy", args.output_dir)

        with open(args.transcript, "r") as f:
            transcript_data = json.load(f)

        title = Path(args.transcript).stem.replace("_transcript", "")
        segments = preparer.extract_speaker_segments(transcript_data, args.audio, title)
        reference_clips = preparer.organize_reference_clips(segments)

        config_path = Path(args.output_dir) / f"{title}_config.yaml"
        preparer.generate_config_yaml(reference_clips, str(config_path))

        print(f"✅ Reference clips extracted and config generated: {config_path}")

    elif args.command == "generate-config":
        # Generate config from existing reference clips
        data_dir = Path(args.data_dir)
        ref_clips_dir = data_dir / "reference_clips"

        if not ref_clips_dir.exists():
            print(f"Error: reference_clips directory not found: {ref_clips_dir}")
            return

        # Scan for speaker directories
        reference_clips = {}
        for speaker_dir in ref_clips_dir.iterdir():
            if speaker_dir.is_dir():
                speaker_id = speaker_dir.name
                audio_files = list(speaker_dir.glob("*.wav"))
                if audio_files:
                    reference_clips[speaker_id] = [str(f) for f in audio_files]

        if not reference_clips:
            print("Error: No reference clips found")
            return

        preparer = DataPreparer("dummy", args.data_dir)
        preparer.generate_config_yaml(reference_clips, args.output)

        print(f"✅ Config generated: {args.output}")


if __name__ == "__main__":
    main()
