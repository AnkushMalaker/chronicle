#!/usr/bin/env python3
"""
ASR client using Wyoming protocol.
Captures audio from microphone or file and sends to ASR service for transcription.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from easy_audio_interfaces.audio_interfaces import ResamplingBlock
from easy_audio_interfaces.filesystem import LocalFileStreamer
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

logger = logging.getLogger(__name__)

SAMP_RATE = 16000
CHANNELS = 1
SAMP_WIDTH = 2  # bytes (16-bit)
MIN_RECORDING_SECONDS = 5  # Minimum time to record before allowing stop


def ensure_txt_extension(output_path: str) -> Path:
    """Ensure the output file has a .txt extension."""
    path = Path(output_path)
    if not path.suffix:
        path = path.with_suffix(".txt")
    elif path.suffix.lower() != ".txt":
        path = path.with_suffix(path.suffix + ".txt")
    return path


async def write_transcript(text: str, output_file: Path | None = None):
    """Write transcript to console and optionally to file."""
    print(f"Transcript: {text}")

    if output_file:
        try:
            # Append to file (create if doesn't exist)
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(f"{text}\n")
            logger.info(f"Transcript written to: {output_file}")
        except Exception as e:
            logger.error(f"Failed to write to output file {output_file}: {e}")


async def run_mic_transcription(
    asr_url: str, device_index: int | None = None, output_file: Path | None = None
):
    """Run ASR transcription from microphone input (original behavior)."""
    # Import here to only need pyaudio when calling inputmicstream
    from easy_audio_interfaces.extras.local_audio import InputMicStream

    print(f"Connecting to ASR service: {asr_url}")
    async with AsyncTcpClient.from_uri(asr_url) as client:
        print("Connected to ASR service")

        # Initialize ASR session
        await client.write_event(Transcribe().event())
        await client.write_event(
            AudioStart(rate=SAMP_RATE, width=SAMP_WIDTH, channels=CHANNELS).event()
        )

        async def mic():
            try:
                async with InputMicStream(
                    sample_rate=SAMP_RATE,
                    channels=CHANNELS,
                    chunk_size=512,
                    device_index=device_index,
                ) as stream:
                    logger.info(
                        f"Starting microphone capture from device: {device_index or 'default'}"
                    )
                    while True:
                        data = await stream.read()
                        await client.write_event(data.event())
                        logger.debug(f"Sent audio chunk: {len(data.audio)} bytes")
                        await asyncio.sleep(0.01)
            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.info("Stopping microphone capture...")
                return

        async def transcriptions():
            try:
                while True:
                    event = await client.read_event()
                    if event is None:
                        break
                    if Transcript.is_type(event.type):
                        transcript = Transcript.from_event(event)
                        await write_transcript(transcript.text, output_file)
                    else:
                        logger.debug(f"Received event: {event}")
            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.info("Stopping transcription reader...")
                return

        try:
            await asyncio.gather(mic(), transcriptions(), return_exceptions=True)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nStopping ASR client...")
        print("ASR client stopped.")


async def run_mic_transcription_with_timing(
    asr_url: str, device_index: int | None = None, output_file: Path | None = None
):
    """Run ASR transcription from microphone with proper timing and audio-stop signaling."""
    # Import here to only need pyaudio when calling inputmicstream
    from easy_audio_interfaces.extras.local_audio import InputMicStream

    print(f"Connecting to ASR service: {asr_url}")
    async with AsyncTcpClient.from_uri(asr_url) as client:
        print("Connected to ASR service")

        # Initialize ASR session according to Wyoming protocol
        await client.write_event(Transcribe().event())
        await client.write_event(
            AudioStart(rate=SAMP_RATE, width=SAMP_WIDTH, channels=CHANNELS).event()
        )

        recording_start_time = asyncio.get_event_loop().time()
        stop_recording = asyncio.Event()

        async def send_audio_and_stop():
            """Send audio for minimum duration then signal stop."""
            try:
                async with InputMicStream(
                    sample_rate=SAMP_RATE,
                    channels=CHANNELS,
                    chunk_size=512,
                    device_index=device_index,
                ) as stream:
                    logger.info(
                        f"Starting microphone capture from device: {device_index or 'default'}"
                    )
                    print(
                        f"Recording... (will stop after {MIN_RECORDING_SECONDS} seconds or Ctrl+C)"
                    )

                    while not stop_recording.is_set():
                        current_time = asyncio.get_event_loop().time()
                        elapsed = current_time - recording_start_time

                        data = await stream.read()
                        await client.write_event(data.event())
                        logger.debug(f"Sent audio chunk: {len(data.audio)} bytes")

                        # After minimum time, automatically stop
                        if elapsed >= MIN_RECORDING_SECONDS:
                            stop_recording.set()
                            break

                    # Send audio-stop to signal ASR to finalize transcript
                    elapsed = asyncio.get_event_loop().time() - recording_start_time
                    print(f"Sending audio stop signal after {elapsed:.1f} seconds...")
                    await client.write_event(AudioStop().event())
                    logger.info("Audio stop signal sent")

            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.info("Stopping microphone capture...")
                # Send audio-stop even on interruption
                try:
                    elapsed = asyncio.get_event_loop().time() - recording_start_time
                    print(
                        f"Interrupted after {elapsed:.1f} seconds, sending audio stop..."
                    )
                    await client.write_event(AudioStop().event())
                    logger.info("Audio stop signal sent on interruption")
                except:
                    pass
                return

        async def receive_transcriptions():
            """Receive and display transcriptions from the ASR service."""
            try:
                while True:
                    event = await client.read_event()
                    if event is None:
                        break
                    if Transcript.is_type(event.type):
                        transcript = Transcript.from_event(event)
                        await write_transcript(transcript.text, output_file)
                        # After receiving transcript, we can exit
                        stop_recording.set()
                        return
                    else:
                        logger.debug(f"Received event: {event}")
            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.info("Stopping transcription reader...")
                stop_recording.set()
                return

        try:
            await asyncio.gather(
                send_audio_and_stop(), receive_transcriptions(), return_exceptions=True
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nStopping ASR client...")
        print("ASR client stopped.")


async def run_file_transcription(
    asr_url: str, file_path: str | Path, output_file: Path | None = None
):
    """Run ASR transcription from audio file input."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    send_start_time: float
    send_end_time: float
    TIMEOUT_SECONDS = 10

    stop_event = asyncio.Event()

    resampler = ResamplingBlock(resample_rate=16000, resample_channels=1)

    print(f"Connecting to ASR service: {asr_url}")
    async with AsyncTcpClient.from_uri(asr_url) as client:
        print("Connected to ASR service")

        # Initialize file streamer to get audio properties
        async with LocalFileStreamer(
            file_path=file_path, chunk_size_samples=512
        ) as streamer, resampler:
            print(f"Loaded audio file: {file_path}")
            print(f"Sample rate: {streamer.sample_rate}, Channels: {streamer.channels}")

            # Initialize ASR session with file's audio properties
            await client.write_event(Transcribe().event())
            await client.write_event(
                AudioStart(
                    rate=streamer.sample_rate,
                    width=SAMP_WIDTH,  # wtf???
                    channels=streamer.channels,
                ).event()
            )
            send_start_time = asyncio.get_event_loop().time()

            async def file_reader():
                nonlocal send_end_time
                try:
                    logger.info(f"Starting file transcription: {file_path}")
                    async for chunk in streamer.iter_frames():
                        async for c in resampler.process_chunk(chunk):
                            await client.write_event(c.event())
                            logger.debug(f"Sent audio chunk: {len(c.audio)} bytes")
                            await asyncio.sleep(
                                0
                            )  # No delay to send audio as fast as possible
                    logger.info("Finished sending audio file")
                    send_end_time = asyncio.get_event_loop().time()
                    logger.info(
                        f"Finished sending audio file in {send_end_time - send_start_time:.2f} seconds"
                    )

                    # Send AudioStop to signal end of stream
                    await client.write_event(AudioStop().event())
                    logger.info("Sent AudioStop event")

                    await asyncio.sleep(TIMEOUT_SECONDS)
                    stop_event.set()
                except (KeyboardInterrupt, asyncio.CancelledError):
                    logger.info("Stopping file reader...")
                    return
                except Exception as e:
                    logger.error(f"Error reading file: {e}")
                    raise

            async def transcriptions():
                nonlocal send_end_time
                try:
                    while not stop_event.is_set():
                        event = await client.read_event()
                        if event is None:
                            break
                        if Transcript.is_type(event.type):
                            transcript = Transcript.from_event(event)
                            await write_transcript(transcript.text, output_file)
                            logger.info(
                                f"Transcript received in {asyncio.get_event_loop().time() - send_end_time:.2f} seconds"
                            )
                        else:
                            logger.debug(f"Received event: {event}")
                except (KeyboardInterrupt, asyncio.CancelledError):
                    logger.info("Stopping transcription reader...")
                    return

            try:
                await asyncio.gather(
                    file_reader(), transcriptions(), return_exceptions=True
                )
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\nStopping ASR client...")
            print("ASR client stopped.")


async def main():
    parser = argparse.ArgumentParser(
        description="ASR client using Wyoming protocol",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s mic                      # Use default microphone (original behavior)
  %(prog)s mic --timed              # Record for 5 seconds then get transcript (recommended)
  %(prog)s mic --device 1           # Use specific audio device
  %(prog)s mic --device 1 --timed   # Use specific device with timed recording
  %(prog)s mic --timed -o output    # Save transcript to output.txt
  %(prog)s file audio.wav           # Transcribe audio file
  %(prog)s file audio.wav -o result # Save transcript to result.txt
        """,
    )

    parser.add_argument(
        "--asr-url",
        type=str,
        default="tcp://192.168.0.110:8765",
        help="ASR service URL (default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v: INFO, -vv: DEBUG)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output file to write transcripts (.txt will be added if no extension)",
    )

    # Create subparsers for different input modes
    subparsers = parser.add_subparsers(
        dest="command", help="Input source for audio", required=True
    )

    # Microphone subcommand
    mic_parser = subparsers.add_parser("mic", help="Capture audio from microphone")
    mic_parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="Audio input device index (default: system default)",
    )
    mic_parser.add_argument(
        "--timed",
        action="store_true",
        help=f"Record for {MIN_RECORDING_SECONDS} seconds then send audio-stop to get transcript (recommended)",
    )

    # File subcommand
    file_parser = subparsers.add_parser("file", help="Transcribe audio from file")
    file_parser.add_argument(
        "file_path", type=str, help="Path to audio file (e.g., audio.wav)"
    )

    args = parser.parse_args()

    # Set up logging
    loglevel = logging.WARNING - (10 * min(args.verbose, 2))
    logging.basicConfig(
        format="%(asctime)s  %(levelname)s  %(message)s", level=loglevel
    )

    # Process output file if specified
    output_file = None
    if args.output:
        output_file = ensure_txt_extension(args.output)
        print(f"Transcripts will be written to: {output_file}")
        # Clear the file if it exists (start fresh)
        try:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, "w", encoding="utf-8") as f:
                f.write("")  # Clear file
        except Exception as e:
            logger.error(f"Failed to initialize output file {output_file}: {e}")
            return 1

    try:
        if args.command == "mic":
            if getattr(args, "timed", False):
                await run_mic_transcription_with_timing(
                    args.asr_url, args.device, output_file
                )
            else:
                await run_mic_transcription(args.asr_url, args.device, output_file)
        elif args.command == "file":
            await run_file_transcription(args.asr_url, args.file_path, output_file)
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        logger.error(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
