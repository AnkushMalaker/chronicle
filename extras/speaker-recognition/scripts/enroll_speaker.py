#!/usr/bin/env python3
"""
Helper script for enrolling speakers in the speaker recognition system.

Usage:
    # Enroll from single audio file
    python enroll_speaker.py --file my_voice.wav --id ankush --name "Ankush"

    # Enroll from multiple files (better accuracy)
    python enroll_speaker.py --files voice1.wav voice2.wav voice3.wav --id ankush --name "Ankush"

    # Enroll from directory of audio files
    python enroll_speaker.py --dir ~/voice_samples/ --id ankush --name "Ankush"

    # Enroll from YouTube video with timestamp
    python enroll_speaker.py --youtube "https://youtube.com/watch?v=..." --start 10 --end 60 --id ankush --name "Ankush"

    # List enrolled speakers
    python enroll_speaker.py --list

    # Delete a speaker
    python enroll_speaker.py --delete ankush
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Default speaker service URL
SPEAKER_SERVICE_URL = os.getenv("SPEAKER_SERVICE_URL", "http://localhost:8085")


def check_service_health_with_url(service_url):
    """Check if speaker service is running."""
    try:
        response = requests.get(f"{service_url}/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            logger.info(
                f"✅ Speaker service is running (Device: {data.get('device', 'unknown')}, Speakers: {data.get('speakers', 0)})"
            )
            return True
    except Exception as e:
        logger.error(f"❌ Cannot connect to speaker service at {service_url}: {e}")
        logger.info(
            "Make sure the speaker service is running: docker compose up speaker-recognition"
        )
    return False


def check_service_health():
    """Check if speaker service is running."""
    return check_service_health_with_url(SPEAKER_SERVICE_URL)


def enroll_single_file(
    file_path: str,
    speaker_id: str,
    speaker_name: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    service_url: str = None,
) -> bool:
    """Enroll speaker from a single audio file."""
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return False

    logger.info(f"Enrolling from file: {file_path}")

    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "audio/wav")}
            data = {"speaker_id": speaker_id, "speaker_name": speaker_name}

            if start is not None:
                data["start"] = start
            if end is not None:
                data["end"] = end

            response = requests.post(
                f"{SPEAKER_SERVICE_URL}/enroll/upload", files=files, data=data
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("updated"):
                    logger.info(
                        f"✅ Updated existing speaker: {speaker_name} (ID: {speaker_id})"
                    )
                else:
                    logger.info(
                        f"✅ Enrolled new speaker: {speaker_name} (ID: {speaker_id})"
                    )
                return True
            else:
                logger.error(
                    f"❌ Enrollment failed: {response.status_code} - {response.text}"
                )
                return False

    except Exception as e:
        logger.error(f"❌ Error during enrollment: {e}")
        return False


def enroll_multiple_files(
    file_paths: List[str], speaker_id: str, speaker_name: str
) -> bool:
    """Enroll speaker from multiple audio files for better accuracy."""
    valid_files = [f for f in file_paths if os.path.exists(f)]

    if not valid_files:
        logger.error("No valid files found")
        return False

    logger.info(f"Enrolling from {len(valid_files)} files...")

    try:
        files = []
        for file_path in valid_files:
            with open(file_path, "rb") as f:
                content = f.read()
                files.append(
                    ("files", (os.path.basename(file_path), content, "audio/wav"))
                )

        data = {"speaker_id": speaker_id, "speaker_name": speaker_name}

        response = requests.post(
            f"{SPEAKER_SERVICE_URL}/enroll/batch", files=files, data=data
        )

        if response.status_code == 200:
            result = response.json()
            logger.info(
                f"✅ Successfully enrolled {speaker_name} using {result.get('num_segments', 0)} segments from {result.get('num_files', 0)} files"
            )
            return True
        else:
            logger.error(
                f"❌ Batch enrollment failed: {response.status_code} - {response.text}"
            )
            return False

    except Exception as e:
        logger.error(f"❌ Error during batch enrollment: {e}")
        return False


def enroll_from_directory(directory: str, speaker_id: str, speaker_name: str) -> bool:
    """Enroll speaker from all audio files in a directory."""
    audio_extensions = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}
    dir_path = Path(directory)

    if not dir_path.exists():
        logger.error(f"Directory not found: {directory}")
        return False

    audio_files = [
        str(f) for f in dir_path.iterdir() if f.suffix.lower() in audio_extensions
    ]

    if not audio_files:
        logger.error(f"No audio files found in {directory}")
        return False

    logger.info(f"Found {len(audio_files)} audio files in {directory}")
    return enroll_multiple_files(audio_files, speaker_id, speaker_name)


def download_youtube_audio(
    url: str, start: Optional[float] = None, end: Optional[float] = None
) -> Optional[str]:
    """Download audio from YouTube video."""
    try:
        import yt_dlp
    except ImportError:
        logger.error("yt-dlp not installed. Install with: pip install yt-dlp")
        return None

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        output_path = tmp_file.name

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path.replace(".wav", ".%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "192",
            }
        ],
    }

    try:
        logger.info(f"Downloading audio from YouTube: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = info.get("duration", 0)

            if start is not None and end is not None:
                logger.info(f"Extracting segment from {start}s to {end}s")
                # Use ffmpeg to extract segment
                segment_path = output_path.replace(".wav", "_segment.wav")
                cmd = [
                    "ffmpeg",
                    "-i",
                    output_path,
                    "-ss",
                    str(start),
                    "-to",
                    str(end),
                    "-c",
                    "copy",
                    segment_path,
                    "-y",
                ]
                subprocess.run(cmd, capture_output=True, check=True, timeout=60)
                os.unlink(output_path)
                return segment_path

            return output_path

    except Exception as e:
        logger.error(f"❌ Error downloading YouTube audio: {e}")
        return None


def list_speakers() -> bool:
    """List all enrolled speakers."""
    try:
        response = requests.get(f"{SPEAKER_SERVICE_URL}/speakers")
        if response.status_code == 200:
            data = response.json()
            speakers = data.get("speakers", {})

            if not speakers:
                logger.info("No speakers enrolled yet")
                return True

            logger.info(f"\n{'='*60}")
            logger.info(f"Enrolled Speakers ({len(speakers)} total)")
            logger.info(f"{'='*60}")

            for speaker_id, info in speakers.items():
                logger.info(f"ID: {speaker_id}")
                logger.info(f"  Name: {info.get('name', 'Unknown')}")
                logger.info(f"  FAISS Index: {info.get('faiss_index', 'N/A')}")
                logger.info("")

            return True
        else:
            logger.error(f"❌ Failed to list speakers: {response.status_code}")
            return False

    except Exception as e:
        logger.error(f"❌ Error listing speakers: {e}")
        return False


def delete_speaker(speaker_id: str) -> bool:
    """Delete an enrolled speaker."""
    try:
        response = requests.delete(f"{SPEAKER_SERVICE_URL}/speakers/{speaker_id}")
        if response.status_code == 200:
            logger.info(f"✅ Successfully deleted speaker: {speaker_id}")
            return True
        elif response.status_code == 404:
            logger.error(f"❌ Speaker not found: {speaker_id}")
            return False
        else:
            logger.error(f"❌ Failed to delete speaker: {response.status_code}")
            return False

    except Exception as e:
        logger.error(f"❌ Error deleting speaker: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Enroll speakers in the speaker recognition system"
    )

    # Action arguments
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--file", help="Enroll from single audio file")
    action_group.add_argument(
        "--files", nargs="+", help="Enroll from multiple audio files"
    )
    action_group.add_argument("--dir", help="Enroll from directory of audio files")
    action_group.add_argument("--youtube", help="Enroll from YouTube video URL")
    action_group.add_argument(
        "--list", action="store_true", help="List enrolled speakers"
    )
    action_group.add_argument("--delete", help="Delete a speaker by ID")

    # Speaker info arguments
    parser.add_argument("--id", help="Speaker ID (required for enrollment)")
    parser.add_argument("--name", help="Speaker display name (required for enrollment)")

    # Optional arguments
    parser.add_argument(
        "--start", type=float, help="Start time in seconds (for file/youtube)"
    )
    parser.add_argument(
        "--end", type=float, help="End time in seconds (for file/youtube)"
    )
    parser.add_argument(
        "--service-url", default=SPEAKER_SERVICE_URL, help="Speaker service URL"
    )

    args = parser.parse_args()

    # Update service URL if provided
    service_url = args.service_url

    # Check service health
    if not check_service_health_with_url(service_url):
        return 1

    # Handle actions
    if args.list:
        return 0 if list_speakers_with_url(service_url) else 1

    elif args.delete:
        return 0 if delete_speaker_with_url(args.delete, service_url) else 1

    else:
        # Enrollment actions require ID and name
        if not args.id or not args.name:
            logger.error("❌ --id and --name are required for enrollment")
            return 1

        if args.file:
            success = enroll_single_file(
                args.file, args.id, args.name, args.start, args.end
            )

        elif args.files:
            success = enroll_multiple_files(args.files, args.id, args.name)

        elif args.dir:
            success = enroll_from_directory(args.dir, args.id, args.name)

        elif args.youtube:
            audio_path = download_youtube_audio(args.youtube, args.start, args.end)
            if audio_path:
                success = enroll_single_file(audio_path, args.id, args.name)
                os.unlink(audio_path)  # Clean up temporary file
            else:
                success = False

        return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
