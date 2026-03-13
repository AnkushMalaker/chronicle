#!/usr/bin/env python3
"""
Speaker Recognition Laptop Client

A client application that uses microphone input to enroll speakers or identify them
using the speaker recognition service.

Usage:
    # Enroll a speaker
    python laptop_client.py enroll --speaker-id "john_doe" --speaker-name "John Doe" --duration 10

    # Identify a speaker
    python laptop_client.py identify --duration 5

    # List enrolled speakers
    python laptop_client.py list

    # Remove a speaker
    python laptop_client.py remove --speaker-id "john_doe"

Requirements:
    # Main dependencies are included in pyproject.toml
    # Only need PyAudio for microphone access:
    # Ubuntu/Debian: sudo apt-get install portaudio19-dev python3-pyaudio
    # macOS: brew install portaudio && pip install pyaudio
    # Windows: pip install pyaudio
"""

import argparse
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

import aiohttp
from easy_audio_interfaces.extras.local_audio import InputMicStream
from easy_audio_interfaces.filesystem.filesystem_interfaces import LocalFileSink

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("laptop_client")


class SpeakerClient:
    """Client for speaker recognition service with microphone recording."""

    def __init__(self, service_url: str = "http://localhost:8001"):
        self.service_url = service_url.rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _request(self, method: str, endpoint: str, **kwargs):
        """Make HTTP request to speaker service."""
        if not self.session:
            raise RuntimeError("Client not initialized")

        url = f"{self.service_url}{endpoint}"
        try:
            async with self.session.request(method, url, **kwargs) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")
        except aiohttp.ClientError as e:
            raise Exception(f"Connection error: {e}")

    async def health_check(self):
        """Check service health."""
        health_response = await self._request("GET", "/health")
        logger.info(f"Health check response: {health_response}")
        return health_response

    async def enroll_speaker(self, speaker_id: str, speaker_name: str, audio_path: str):
        """Enroll a speaker from audio file by uploading it."""
        if not self.session:
            raise RuntimeError("Client not initialized")

        # Prepare query parameters
        params = {"speaker_id": speaker_id, "speaker_name": speaker_name}

        # Prepare file upload
        with open(audio_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field(
                "file", f, filename="recording.wav", content_type="audio/wav"
            )

            url = f"{self.service_url}/enroll/upload"
            async with self.session.post(url, params=params, data=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

    async def identify_speaker(self, audio_path: str) -> dict[str, Any]:
        """Identify speaker from audio file by uploading it."""
        if not self.session:
            raise RuntimeError("Client not initialized")

        # Prepare file upload
        with open(audio_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field(
                "file", f, filename="recording.wav", content_type="audio/wav"
            )

            url = f"{self.service_url}/identify/upload"
            async with self.session.post(url, data=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

    async def verify_speaker(self, speaker_id: str, audio_path: str):
        """Verify if audio matches specific speaker by uploading it."""
        # For now, copy file to a shared location accessible by Docker
        # TODO: Add /verify/upload endpoint to speaker service
        import shutil

        shared_path = f"/tmp/audio_upload_{Path(audio_path).name}"
        shutil.copy2(audio_path, shared_path)

        data = {"speaker_id": speaker_id, "audio_path": shared_path}
        result = await self._request("POST", "/verify", json=data)

        # Clean up shared file
        Path(shared_path).unlink(missing_ok=True)
        return result

    async def diarize_audio(
        self,
        audio_path: str,
        min_duration: Optional[float] = None,
        max_speakers: Optional[int] = None,
    ) -> dict[str, Any]:
        """Perform speaker diarization on audio file by uploading it."""
        if not self.session:
            raise RuntimeError("Client not initialized")

        # Prepare query parameters
        params = {}
        if min_duration is not None:
            params["min_duration"] = min_duration
        if max_speakers is not None:
            params["max_speakers"] = max_speakers

        # Prepare file upload
        with open(audio_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field(
                "file", f, filename="recording.wav", content_type="audio/wav"
            )

            url = f"{self.service_url}/diarize/upload"
            async with self.session.post(url, params=params, data=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

    async def list_speakers(self):
        """List all enrolled speakers."""
        result = await self._request("GET", "/speakers")
        return result.get("speakers", [])

    async def remove_speaker(self, speaker_id: str):
        """Remove an enrolled speaker."""
        return await self._request("DELETE", f"/speakers/{speaker_id}")


async def record_audio(duration: float, sample_rate: int = 16000) -> str:
    """Record audio from microphone and save to temporary file."""
    # Create temporary file
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_path = temp_file.name
    temp_file.close()

    logger.info(f"Recording {duration} seconds of audio...")
    logger.info("Speak now! (Press Ctrl+C to stop recording early)")

    mic = None
    sink = None

    try:
        mic = InputMicStream(sample_rate=sample_rate)
        sink = LocalFileSink(temp_path, sample_rate=sample_rate, channels=1)

        await mic.open()
        await sink.open()

        # Record for specified duration
        start_time = asyncio.get_event_loop().time()
        try:
            async for chunk in mic.iter_frames():
                await sink.write(chunk)

                # Check if duration elapsed
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= duration:
                    break
        except KeyboardInterrupt:
            elapsed = asyncio.get_event_loop().time() - start_time
            logger.info(f"Recording stopped by user after {elapsed:.1f} seconds")

        # Properly close streams
        if sink:
            await sink.close()
        if mic:
            await mic.close()

        logger.info(f"Recording complete. Saved to: {temp_path}")
        return temp_path

    except Exception as e:
        # Properly close streams on error
        if sink:
            try:
                await sink.close()
            except:
                pass
        if mic:
            try:
                await mic.close()
            except:
                pass

        # Clean up on error
        Path(temp_path).unlink(missing_ok=True)
        raise e


def save_audio(temp_path: str, save_path: str):
    """Save recorded audio to specified path."""
    import shutil

    save_path_obj = Path(save_path)
    save_path_obj.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(temp_path, save_path_obj)
    logger.info(f"Audio saved to: {save_path_obj}")


async def cmd_enroll(args):
    """Enroll a new speaker."""
    logger.info(f"Enrolling speaker: {args.speaker_name} (ID: {args.speaker_id})")

    # Determine audio source
    if args.from_file:
        # Use existing file
        if not Path(args.from_file).exists():
            logger.error(f"File not found: {args.from_file}")
            return
        audio_path = args.from_file
        cleanup_audio = False
        logger.info(f"Using audio file: {audio_path}")
    else:
        # Record audio from microphone
        audio_path = await record_audio(args.duration)
        cleanup_audio = True

    try:
        async with SpeakerClient(args.service_url) as client:
            # Check service health
            await client.health_check()

            # Enroll speaker
            result = await client.enroll_speaker(
                args.speaker_id, args.speaker_name, audio_path
            )
            logger.info(f"Server response: {result}")

            # Check for success - server returns {"updated": bool, "speaker_id": str}
            if "speaker_id" in result:
                updated = result.get("updated", False)
                if updated:
                    logger.info(
                        f"✅ Successfully updated existing speaker: {args.speaker_name}"
                    )
                else:
                    logger.info(
                        f"✅ Successfully enrolled new speaker: {args.speaker_name}"
                    )

                # Save audio if requested (only for recorded audio)
                if cleanup_audio and args.save_file:
                    save_audio(audio_path, args.save_file)
            else:
                logger.error(
                    f"❌ Failed to enroll speaker - unexpected response: {result}"
                )

    except Exception as e:
        logger.error(f"Error during enrollment: {e}")
    finally:
        # Clean up temporary file only if we recorded it
        if cleanup_audio:
            Path(audio_path).unlink(missing_ok=True)


async def cmd_identify(args):
    """Identify a speaker from microphone."""
    logger.info("Recording audio for speaker identification...")

    # Record audio
    audio_path = await record_audio(args.duration)

    try:
        async with SpeakerClient(args.service_url) as client:
            # Check service health
            await client.health_check()

            # Identify speaker
            result = await client.identify_speaker(audio_path)

            if result.get("identified"):
                speaker = result.get("speaker", {})
                score = result.get("score", 0.0)
                logger.info(
                    f"✅ Identified speaker: {speaker.get('name', 'Unknown')} (ID: {speaker.get('id', 'Unknown')}) - Score: {score:.3f}"
                )
            else:
                score = result.get("score", 0.0)
                logger.info(f"❓ Speaker not recognized - Score: {score:.3f}")

            # Save audio if requested
            if args.save_file:
                save_audio(audio_path, args.save_file)

    except Exception as e:
        logger.error(f"Error during identification: {e}")
    finally:
        # Clean up temporary file
        Path(audio_path).unlink(missing_ok=True)


async def cmd_verify(args):
    """Verify if audio matches a specific speaker."""
    logger.info(f"Recording audio for verification against speaker: {args.speaker_id}")

    # Record audio
    audio_path = await record_audio(args.duration)

    try:
        async with SpeakerClient(args.service_url) as client:
            # Check service health
            await client.health_check()

            # Verify speaker
            result = await client.verify_speaker(args.speaker_id, audio_path)

            match = result.get("match", False)
            score = result.get("score", 0.0)

            if match:
                logger.info(f"✅ Voice matches {args.speaker_id} - Score: {score:.3f}")
            else:
                logger.info(
                    f"❌ Voice does not match {args.speaker_id} - Score: {score:.3f}"
                )

            # Save audio if requested
            if args.save_file:
                save_audio(audio_path, args.save_file)

    except Exception as e:
        logger.error(f"Error during verification: {e}")
    finally:
        # Clean up temporary file
        Path(audio_path).unlink(missing_ok=True)


async def cmd_list(args):
    """List all enrolled speakers."""
    try:
        async with SpeakerClient(args.service_url) as client:
            # Check service health
            await client.health_check()

            # List speakers
            speakers_dict = await client.list_speakers()
            logger.info(f"Server response: {speakers_dict}")

            if speakers_dict:
                logger.info(f"📋 Enrolled speakers ({len(speakers_dict)}):")
                for speaker_id, speaker_data in speakers_dict.items():
                    name = speaker_data.get("name", "Unknown")
                    logger.info(f"  • {name} (ID: {speaker_id})")
            else:
                logger.info("📋 No speakers enrolled")

    except Exception as e:
        logger.error(f"Error listing speakers: {e}")


async def cmd_remove(args):
    """Remove an enrolled speaker."""
    logger.info(f"Removing speaker: {args.speaker_id}")

    try:
        async with SpeakerClient(args.service_url) as client:
            # Check service health
            await client.health_check()

            # Remove speaker
            result = await client.remove_speaker(args.speaker_id)

            if result.get("deleted"):
                logger.info(f"✅ Successfully removed speaker: {args.speaker_id}")
            else:
                logger.error(f"❌ Failed to remove speaker: {args.speaker_id}")

    except Exception as e:
        logger.error(f"Error removing speaker: {e}")


async def cmd_diarize(args):
    """Perform speaker diarization on audio."""
    logger.info("Recording audio for speaker diarization...")

    # Determine audio source
    if args.from_file:
        # Use existing file
        if not Path(args.from_file).exists():
            logger.error(f"File not found: {args.from_file}")
            return
        audio_path = args.from_file
        cleanup_audio = False
        logger.info(f"Using audio file: {audio_path}")
    else:
        # Record audio from microphone
        audio_path = await record_audio(args.duration)
        cleanup_audio = True

    try:
        async with SpeakerClient(args.service_url) as client:
            # Check service health
            await client.health_check()

            # Perform diarization
            result = await client.diarize_audio(
                audio_path,
                min_duration=args.min_duration,
                max_speakers=args.max_speakers,
            )

            # Display results
            segments = result.get("segments", [])
            summary = result.get("summary", {})

            logger.info(f"🎯 Diarization Results:")
            logger.info(
                f"   Total duration: {summary.get('total_duration', 0):.2f} seconds"
            )
            logger.info(f"   Number of speakers: {summary.get('num_speakers', 0)}")
            logger.info(f"   Number of segments: {summary.get('num_segments', 0)}")

            # Show speaker statistics
            speaker_stats = summary.get("speaker_stats", {})
            if speaker_stats:
                logger.info(f"\n📊 Speaker Statistics:")
                for speaker_id, stats in speaker_stats.items():
                    logger.info(
                        f"   {speaker_id}: {stats['total_duration']}s ({stats['percentage']}%) - {stats['segment_count']} segments"
                    )

            # Show detailed timeline if requested
            if args.show_timeline:
                logger.info(f"\n⏰ Timeline:")
                for segment in segments:
                    start = segment["start"]
                    end = segment["end"]
                    speaker = segment["speaker"]
                    duration = segment["duration"]
                    logger.info(
                        f"   {start:6.2f}s - {end:6.2f}s ({duration:5.2f}s): {speaker}"
                    )

            # Save audio if requested (only for recorded audio)
            if cleanup_audio and args.save_file:
                save_audio(audio_path, args.save_file)

    except Exception as e:
        logger.error(f"Error during diarization: {e}")
    finally:
        # Clean up temporary file only if we recorded it
        if cleanup_audio:
            Path(audio_path).unlink(missing_ok=True)


def main():
    """Main CLI interface."""
    parser = argparse.ArgumentParser(
        description="Speaker Recognition Laptop Client",
        epilog="Examples:\n"
        "  python laptop_client.py enroll --speaker-id john --speaker-name 'John Doe' --duration 10\n"
        "  python laptop_client.py enroll --speaker-id john --speaker-name 'John Doe' --from-file audio.wav\n"
        "  python laptop_client.py identify --duration 5 --save-file identification.wav\n"
        "  python laptop_client.py verify --speaker-id john --duration 3\n"
        "  python laptop_client.py diarize --duration 15 --show-timeline\n"
        "  python laptop_client.py diarize --from-file meeting.wav --max-speakers 3\n"
        "  python laptop_client.py list\n"
        "  python laptop_client.py remove --speaker-id john",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--service-url",
        default="http://localhost:8005",
        help="Speaker recognition service URL (default: http://localhost:8005)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Enroll command
    enroll_parser = subparsers.add_parser("enroll", help="Enroll a new speaker")
    enroll_parser.add_argument("--speaker-id", required=True, help="Unique speaker ID")
    enroll_parser.add_argument(
        "--speaker-name", required=True, help="Speaker display name"
    )
    enroll_parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Recording duration in seconds (default: 10)",
    )
    enroll_parser.add_argument(
        "--from-file", help="Enroll from existing audio file instead of recording"
    )
    enroll_parser.add_argument(
        "--save-file", help="Save recorded audio to specified file path"
    )

    # Identify command
    identify_parser = subparsers.add_parser("identify", help="Identify a speaker")
    identify_parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Recording duration in seconds (default: 5)",
    )
    identify_parser.add_argument(
        "--save-file", help="Save recorded audio to specified file path"
    )

    # Verify command
    verify_parser = subparsers.add_parser("verify", help="Verify a specific speaker")
    verify_parser.add_argument(
        "--speaker-id", required=True, help="Speaker ID to verify against"
    )
    verify_parser.add_argument(
        "--duration",
        type=float,
        default=3.0,
        help="Recording duration in seconds (default: 3)",
    )
    verify_parser.add_argument(
        "--save-file", help="Save recorded audio to specified file path"
    )

    # List command
    list_parser = subparsers.add_parser("list", help="List enrolled speakers")

    # Remove command
    remove_parser = subparsers.add_parser("remove", help="Remove an enrolled speaker")
    remove_parser.add_argument(
        "--speaker-id", required=True, help="Speaker ID to remove"
    )

    # Diarize command
    diarize_parser = subparsers.add_parser(
        "diarize", help="Perform speaker diarization on audio"
    )
    diarize_parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Recording duration in seconds (default: 10)",
    )
    diarize_parser.add_argument(
        "--from-file",
        help="Perform diarization on existing audio file instead of recording",
    )
    diarize_parser.add_argument(
        "--min-duration", type=float, help="Minimum duration of a segment in seconds"
    )
    diarize_parser.add_argument(
        "--max-speakers", type=int, help="Maximum number of speakers to detect"
    )
    diarize_parser.add_argument(
        "--show-timeline",
        action="store_true",
        help="Show detailed timeline of segments",
    )
    diarize_parser.add_argument(
        "--save-file", help="Save diarized audio to specified file path"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Run the appropriate command
    try:
        if args.command == "enroll":
            asyncio.run(cmd_enroll(args))
        elif args.command == "identify":
            asyncio.run(cmd_identify(args))
        elif args.command == "verify":
            asyncio.run(cmd_verify(args))
        elif args.command == "list":
            asyncio.run(cmd_list(args))
        elif args.command == "remove":
            asyncio.run(cmd_remove(args))
        elif args.command == "diarize":
            asyncio.run(cmd_diarize(args))
    except KeyboardInterrupt:
        logger.info("Operation cancelled by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
