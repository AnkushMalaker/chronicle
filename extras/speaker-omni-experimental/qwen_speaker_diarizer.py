#!/usr/bin/env python3
"""
Qwen2.5-Omni Experimental Speaker Recognition & Diarization

A next-generation speaker recognition system using Qwen2.5-Omni's audio understanding
capabilities for enrollment-by-context (few-shot) speaker identification.

Key Features:
- No training required - uses few-shot learning with reference clips
- Closed-set family member identification with real names
- Overlap-tolerant transcription with simultaneous speech support
- Context preservation across multiple audio clips
- Automatic chunking for long audio files

Usage:
    python qwen_speaker_diarizer.py enroll --config config.yaml
    python qwen_speaker_diarizer.py transcribe --audio path/to/audio.wav --config config.yaml
    python qwen_speaker_diarizer.py batch --input-dir path/to/audios --config config.yaml
"""

import argparse
import json
import logging
import os
import tempfile
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch
import yaml
from transformers import Qwen2_5OmniForConditionalGeneration, Qwen2_5OmniProcessor

# Suppress warnings for cleaner output
warnings.filterwarnings("ignore")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class QwenSpeakerDiarizer:
    """
    Qwen2.5-Omni based speaker recognition and diarization system.

    Supports enrollment-by-context for closed-set speaker identification
    with overlap-tolerant transcription and real name output.
    """

    def __init__(
        self, model_id: str = "Qwen/Qwen2.5-Omni-7B", device_map: str = "auto"
    ):
        """
        Initialize the Qwen2.5-Omni diarization system.

        Args:
            model_id: Hugging Face model ID (7B or 3B variant)
            device_map: Device mapping for model loading
        """
        self.model_id = model_id
        self.device_map = device_map
        self.processor = None
        self.model = None
        self.enrolled_conversation = None

        # System prompt for diarization-aware transcription
        self.SYSTEM_PROMPT = (
            "You are a diarization-aware transcriber. Use the *enrolled* voices below.\n"
            "Output one line per segment in this schema:\n"
            "<SEG t_start=SSSS t_end=EEEE speaker=NAME overlap=bool> transcript </SEG>\n"
            "Rules: (1) Use real names exactly as provided for enrolled voices. "
            "(2) If a voice is not enrolled, assign 'Unknown speaker 1', then 2, etc., consistently. "
            "(3) Allow overlapping lines when people talk simultaneously. "
            "(4) Estimate coarse times; do not hallucinate content."
        )

    def load_models(self):
        """Load Qwen2.5-Omni processor and model."""
        logger.info(f"Loading Qwen2.5-Omni model: {self.model_id}")

        try:
            self.processor = Qwen2_5OmniProcessor.from_pretrained(self.model_id)
            self.model = Qwen2_5OmniForConditionalGeneration.from_pretrained(
                self.model_id,
                device_map=self.device_map,
                torch_dtype=torch.bfloat16,
                enable_audio_output=False,  # Text-only generation to save VRAM
            ).eval()
            logger.info("Models loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load models: {e}")
            raise

    def build_enrollment_conversation(
        self, speaker_refs: Dict[str, List[str]]
    ) -> List[Dict]:
        """
        Build enrollment conversation with reference audio clips.

        Args:
            speaker_refs: Dict mapping speaker name -> list of reference audio paths

        Returns:
            Conversation list for consistent speaker context
        """
        conv = [
            {
                "role": "system",
                "content": [{"type": "text", "text": self.SYSTEM_PROMPT}],
            }
        ]

        # Enroll each speaker with their reference clips
        for name, paths in speaker_refs.items():
            logger.info(f"Enrolling speaker: {name} with {len(paths)} reference clips")

            # Verify all reference files exist
            valid_paths = []
            for path in paths:
                if Path(path).exists():
                    valid_paths.append(path)
                else:
                    logger.warning(f"Reference file not found: {path}")

            if not valid_paths:
                logger.error(f"No valid reference files for speaker: {name}")
                continue

            conv.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Reference voice: {name}"},
                        *[{"type": "audio", "path": p} for p in valid_paths],
                    ],
                }
            )
            conv.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Registered {name}."}],
                }
            )

        # Add format example
        conv += [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Format example only:"}],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "<SEG t_start=0.00 t_end=1.10 speaker=Flowerin overlap=false> Hi! </SEG>\n"
                        "<SEG t_start=0.80 t_end=1.90 speaker=Brother overlap=true> Hey! </SEG>",
                    }
                ],
            },
        ]

        self.enrolled_conversation = conv
        logger.info(f"Enrollment complete for {len(speaker_refs)} speakers")
        return conv

    def chunk_audio(
        self, audio_path: str, chunk_duration: int = 30, overlap: int = 5
    ) -> List[str]:
        """
        Split long audio into overlapping chunks.

        Args:
            audio_path: Path to audio file
            chunk_duration: Duration of each chunk in seconds
            overlap: Overlap duration in seconds

        Returns:
            List of temporary chunk file paths
        """
        try:
            import soundfile as sf

            audio_data, sample_rate = sf.read(audio_path)
            duration = len(audio_data) / sample_rate

            if duration <= chunk_duration:
                return [audio_path]  # No chunking needed

            chunk_paths = []
            start_time = 0

            while start_time < duration:
                end_time = min(start_time + chunk_duration, duration)

                start_sample = int(start_time * sample_rate)
                end_sample = int(end_time * sample_rate)

                chunk_data = audio_data[start_sample:end_sample]

                # Create temporary file for chunk
                with tempfile.NamedTemporaryFile(
                    suffix=".wav", delete=False
                ) as temp_file:
                    sf.write(temp_file.name, chunk_data, sample_rate)
                    chunk_paths.append(temp_file.name)

                start_time += chunk_duration - overlap

                if end_time >= duration:
                    break

            logger.info(f"Audio chunked into {len(chunk_paths)} segments")
            return chunk_paths

        except ImportError:
            logger.error(
                "soundfile library required for audio chunking. Install with: pip install soundfile"
            )
            return [audio_path]
        except Exception as e:
            logger.error(f"Failed to chunk audio: {e}")
            return [audio_path]

    def transcribe_with_names(
        self, audio_path: str, chunk_long_audio: bool = True
    ) -> Tuple[str, List[Dict]]:
        """
        Transcribe audio with named speaker diarization.

        Args:
            audio_path: Path to audio file
            chunk_long_audio: Whether to automatically chunk long audio files

        Returns:
            Tuple of (transcript_text, updated_conversation)
        """
        if self.enrolled_conversation is None:
            raise ValueError(
                "No speakers enrolled. Call build_enrollment_conversation first."
            )

        if not Path(audio_path).exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        # Handle long audio by chunking
        if chunk_long_audio:
            chunk_paths = self.chunk_audio(audio_path)
        else:
            chunk_paths = [audio_path]

        full_transcript = []
        conv = self.enrolled_conversation.copy()

        try:
            for i, chunk_path in enumerate(chunk_paths):
                logger.info(f"Processing chunk {i+1}/{len(chunk_paths)}")

                # Prepare conversation with current audio chunk
                chunk_conv = conv + [
                    {
                        "role": "user",
                        "content": [
                            {"type": "audio", "path": chunk_path},
                            {
                                "type": "text",
                                "text": "Transcribe with named diarization; keep IDs consistent with enrolled voices.",
                            },
                        ],
                    }
                ]

                # Apply chat template and generate
                inputs = self.processor.apply_chat_template(
                    chunk_conv,
                    add_generation_prompt=True,
                    tokenize=True,
                    return_tensors="pt",
                    padding=True,
                ).to(self.model.device)

                # Generate text-only response
                with torch.no_grad():
                    text_ids = self.model.generate(
                        **inputs,
                        max_new_tokens=1024,
                        do_sample=False,
                        pad_token_id=self.processor.tokenizer.eos_token_id,
                    )

                # Decode response
                response = self.processor.batch_decode(
                    text_ids[:, inputs["input_ids"].size(1) :], skip_special_tokens=True
                )[0].strip()

                # Append response to conversation for context preservation
                conv.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": response}],
                    }
                )

                full_transcript.append(response)

                # Clean up temporary chunk files
                if chunk_path != audio_path and Path(chunk_path).exists():
                    os.unlink(chunk_path)

            # Combine transcripts from all chunks
            combined_transcript = "\n".join(full_transcript)
            return combined_transcript, conv

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            # Clean up any remaining temp files
            for chunk_path in chunk_paths:
                if chunk_path != audio_path and Path(chunk_path).exists():
                    try:
                        os.unlink(chunk_path)
                    except:
                        pass
            raise

    def save_results(
        self, transcript: str, output_path: str, metadata: Optional[Dict] = None
    ):
        """
        Save transcription results to file.

        Args:
            transcript: Transcribed text with speaker segments
            output_path: Path to save results
            metadata: Optional metadata to include
        """
        results = {
            "transcript": transcript,
            "model": self.model_id,
            "timestamp": str(Path().cwd()),
            "metadata": metadata or {},
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        logger.info(f"Results saved to: {output_path}")


def load_config(config_path: str) -> Dict:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    """Main CLI interface."""
    parser = argparse.ArgumentParser(
        description="Qwen2.5-Omni Speaker Recognition & Diarization"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Enroll command
    enroll_parser = subparsers.add_parser("enroll", help="Enroll speakers from config")
    enroll_parser.add_argument(
        "--config", required=True, help="Configuration YAML file"
    )
    enroll_parser.add_argument(
        "--model", default="Qwen/Qwen2.5-Omni-7B", help="Model ID (7B or 3B)"
    )

    # Transcribe command
    transcribe_parser = subparsers.add_parser(
        "transcribe", help="Transcribe audio with speaker names"
    )
    transcribe_parser.add_argument(
        "--audio", required=True, help="Audio file to transcribe"
    )
    transcribe_parser.add_argument(
        "--config", required=True, help="Configuration YAML file"
    )
    transcribe_parser.add_argument("--output", help="Output file path (optional)")
    transcribe_parser.add_argument(
        "--model", default="Qwen/Qwen2.5-Omni-7B", help="Model ID (7B or 3B)"
    )
    transcribe_parser.add_argument(
        "--no-chunk", action="store_true", help="Disable automatic chunking"
    )

    # Batch command
    batch_parser = subparsers.add_parser(
        "batch", help="Batch process multiple audio files"
    )
    batch_parser.add_argument(
        "--input-dir", required=True, help="Directory containing audio files"
    )
    batch_parser.add_argument("--config", required=True, help="Configuration YAML file")
    batch_parser.add_argument(
        "--output-dir", default="./output", help="Output directory"
    )
    batch_parser.add_argument(
        "--model", default="Qwen/Qwen2.5-Omni-7B", help="Model ID (7B or 3B)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load configuration
    config = load_config(args.config)
    speaker_refs = config.get("speakers", {})

    if not speaker_refs:
        logger.error("No speakers configured in config file")
        return

    # Initialize diarizer
    diarizer = QwenSpeakerDiarizer(model_id=args.model)
    diarizer.load_models()

    if args.command == "enroll":
        # Build enrollment conversation
        conv = diarizer.build_enrollment_conversation(speaker_refs)
        logger.info("Speaker enrollment completed successfully")

    elif args.command == "transcribe":
        # Enroll speakers and transcribe single audio
        diarizer.build_enrollment_conversation(speaker_refs)

        transcript, _ = diarizer.transcribe_with_names(
            args.audio, chunk_long_audio=not args.no_chunk
        )

        print("\n" + "=" * 50)
        print("TRANSCRIPTION RESULT:")
        print("=" * 50)
        print(transcript)
        print("=" * 50 + "\n")

        # Save results if output path specified
        if args.output:
            diarizer.save_results(
                transcript,
                args.output,
                {"audio_file": args.audio, "config": args.config},
            )

    elif args.command == "batch":
        # Batch process multiple files
        input_dir = Path(args.input_dir)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(exist_ok=True)

        # Find audio files
        audio_extensions = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
        audio_files = [
            f
            for f in input_dir.iterdir()
            if f.is_file() and f.suffix.lower() in audio_extensions
        ]

        if not audio_files:
            logger.error(f"No audio files found in {input_dir}")
            return

        logger.info(f"Found {len(audio_files)} audio files to process")

        # Enroll speakers once
        diarizer.build_enrollment_conversation(speaker_refs)

        # Process each file
        for audio_file in audio_files:
            logger.info(f"Processing: {audio_file.name}")

            try:
                transcript, _ = diarizer.transcribe_with_names(str(audio_file))

                # Save results
                output_file = output_dir / f"{audio_file.stem}_transcript.json"
                diarizer.save_results(
                    transcript,
                    str(output_file),
                    {"audio_file": str(audio_file), "config": args.config},
                )

            except Exception as e:
                logger.error(f"Failed to process {audio_file.name}: {e}")
                continue

        logger.info(f"Batch processing complete. Results saved to {output_dir}")


if __name__ == "__main__":
    main()
