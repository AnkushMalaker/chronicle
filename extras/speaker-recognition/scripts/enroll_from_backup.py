#!/usr/bin/env python3
"""
Script to enroll all speakers from the backup directory.

This script reads the backup speaker data and re-enrolls all speakers
using the /enroll/batch endpoint.
"""

import asyncio
import logging
import sys
from pathlib import Path

import aiohttp

# Add the src directory to Python path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger(__name__)

# Configuration
BACKUP_DIR = Path(__file__).parent.parent / "speaker_data_backup"
ENROLLMENT_AUDIO_DIR = BACKUP_DIR / "enrollment_audio" / "1"  # User ID 1
API_BASE_URL = "http://localhost:8085"
TIMEOUT = 300  # 5 minutes per enrollment


async def enroll_speaker_from_backup(
    session: aiohttp.ClientSession, speaker_dir: Path
) -> bool:
    """Enroll a single speaker from backup directory."""

    try:
        # Extract speaker info from directory name
        speaker_id = speaker_dir.name

        # Extract speaker name from ID (remove user_1_ prefix and timestamp suffix)
        speaker_name = speaker_id.replace("user_1_", "").rsplit("_", 1)[0]

        log.info(f"Enrolling speaker: {speaker_name} (ID: {speaker_id})")

        # Find all WAV files in the directory
        audio_files = list(speaker_dir.glob("*.wav"))

        if not audio_files:
            log.error(f"No WAV files found for {speaker_name}, skipping")
            return False

        log.info(f"  Found {len(audio_files)} audio file(s)")
        for audio_file in audio_files:
            log.info(f"    - {audio_file.name}")

        # Prepare multipart form data
        form_data = aiohttp.FormData()
        form_data.add_field("speaker_id", speaker_id)
        form_data.add_field("speaker_name", speaker_name)

        # Add all audio files
        for audio_path in audio_files:
            with open(audio_path, "rb") as f:
                form_data.add_field(
                    "files",
                    f.read(),
                    filename=audio_path.name,
                    content_type="audio/wav",
                )

        # Send enrollment request
        log.info(f"Sending enrollment request for {speaker_name}...")
        async with session.post(
            f"{API_BASE_URL}/enroll/batch",
            data=form_data,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as response:
            if response.status == 200:
                result = await response.json()
                log.info(f"✅ Successfully enrolled {speaker_name}: {result}")
                return True
            else:
                error_text = await response.text()
                log.error(
                    f"❌ Failed to enroll {speaker_name} (HTTP {response.status}): {error_text}"
                )
                return False

    except Exception as e:
        log.error(f"❌ Error enrolling {speaker_dir.name}: {e}")
        return False


async def check_service_health() -> bool:
    """Check if the speaker service is healthy."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_BASE_URL}/health") as response:
                if response.status == 200:
                    health_info = await response.json()
                    log.info(f"Service health: {health_info}")
                    return True
                else:
                    log.error(f"Service health check failed: HTTP {response.status}")
                    return False
    except Exception as e:
        log.error(f"Failed to connect to service: {e}")
        return False


async def main():
    """Main enrollment function."""
    log.info("Starting speaker enrollment from backup directory")
    log.info(f"Backup directory: {BACKUP_DIR}")
    log.info(f"Enrollment audio directory: {ENROLLMENT_AUDIO_DIR}")

    # Check if backup directory exists
    if not ENROLLMENT_AUDIO_DIR.exists():
        log.error(f"Backup directory not found: {ENROLLMENT_AUDIO_DIR}")
        return 1

    # Check service health
    log.info("Checking speaker service health...")
    if not await check_service_health():
        log.error("Speaker service is not healthy, aborting")
        return 1

    # Find all speaker directories
    speaker_dirs = []
    for item in ENROLLMENT_AUDIO_DIR.iterdir():
        if item.is_dir() and item.name.startswith("user_1_"):
            speaker_dirs.append(item)

    if not speaker_dirs:
        log.error("No speaker directories found in backup")
        return 1

    log.info(f"Found {len(speaker_dirs)} speakers to enroll")

    # Enroll each speaker
    success_count = 0
    failed_count = 0

    async with aiohttp.ClientSession() as session:
        for speaker_dir in sorted(speaker_dirs):
            success = await enroll_speaker_from_backup(session, speaker_dir)
            if success:
                success_count += 1
            else:
                failed_count += 1

            # Small delay between enrollments
            await asyncio.sleep(1)

    # Summary
    log.info(f"\n{'='*50}")
    log.info(f"Enrollment Summary:")
    log.info(f"  Successfully enrolled: {success_count}")
    log.info(f"  Failed: {failed_count}")
    log.info(f"  Total: {len(speaker_dirs)}")
    log.info(f"{'='*50}")

    if failed_count > 0:
        log.warning("Some enrollments failed. Check logs above for details.")
        return 1
    else:
        log.info("All speakers enrolled successfully! 🎉")
        return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        log.info("Enrollment interrupted by user")
        sys.exit(1)
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        sys.exit(1)
