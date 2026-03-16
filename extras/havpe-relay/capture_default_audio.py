"""
Capture audio from a default HA Voice PE device running stock firmware.

Acts as a minimal "Home Assistant" voice assistant server using aioesphomeapi.
Connects to the device, subscribes to voice assistant events, and saves
the raw audio stream to a WAV file for comparison.

Usage:
    uv run python capture_default_audio.py <device_ip> [--output audio_capture.wav]

The device must be running the default HA Voice PE firmware with voice_assistant
component. Trigger audio by saying the wake word or pressing the button.
Press Ctrl+C to stop and save.
"""

import argparse
import asyncio
import struct
import sys
import wave
from datetime import datetime

from aioesphomeapi import (
    APIClient,
    VoiceAssistantAudioSettingsModel,
    VoiceAssistantEventType,
)


class AudioCapture:
    def __init__(self, output_path: str):
        self.output_path = output_path
        self.audio_chunks: list[bytes] = []
        self.capturing = False
        self.capture_count = 0

    async def handle_start(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: VoiceAssistantAudioSettingsModel,
        wake_word_phrase: str | None,
    ) -> int | None:
        """Called when device wants to start voice assistant pipeline."""
        print(f"\n--- Voice assistant START ---")
        print(f"  conversation_id: {conversation_id}")
        print(f"  flags: {flags}")
        print(
            f"  audio_settings: noise_suppression={audio_settings.noise_suppression_level}, "
            f"auto_gain={audio_settings.auto_gain}, "
            f"volume_multiplier={audio_settings.volume_multiplier}"
        )
        if wake_word_phrase:
            print(f"  wake_word: {wake_word_phrase}")

        self.capturing = True
        self.capture_count += 1
        print(f"  Capturing audio (session #{self.capture_count})...")

        # Return port 0 = use API audio (not UDP)
        return 0

    async def handle_stop(self, abort: bool) -> None:
        """Called when device stops voice assistant pipeline."""
        if self.capturing:
            print(f"\n--- Voice assistant STOP (abort={abort}) ---")
            print(
                f"  Captured {len(self.audio_chunks)} chunks, "
                f"{sum(len(c) for c in self.audio_chunks)} bytes total"
            )
            self.capturing = False

    async def handle_audio(self, data: bytes) -> None:
        """Called for each audio chunk from the device."""
        self.audio_chunks.append(data)
        if len(self.audio_chunks) % 50 == 0:
            total_bytes = sum(len(c) for c in self.audio_chunks)
            # Assume 16-bit mono 16kHz
            duration = total_bytes / (16000 * 2)
            print(f"  ... {len(self.audio_chunks)} chunks, {duration:.1f}s", end="\r")

    def save_wav(
        self, sample_rate: int = 16000, sample_width: int = 2, channels: int = 1
    ):
        """Save captured audio to WAV file."""
        if not self.audio_chunks:
            print("No audio captured!")
            return

        raw_audio = b"".join(self.audio_chunks)
        total_samples = len(raw_audio) // sample_width
        duration = total_samples / (sample_rate * channels)

        # Analyze levels
        samples = struct.unpack(f"<{total_samples}h", raw_audio)
        peak = max(abs(s) for s in samples) if samples else 0
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0

        with wave.open(self.output_path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(raw_audio)

        print(f"\nSaved {self.output_path}:")
        print(f"  Duration: {duration:.1f}s")
        print(f"  Format: {sample_rate}Hz, {sample_width * 8}-bit, {channels}ch")
        print(f"  Peak: {peak}")
        print(f"  RMS: {rms:.0f}")
        print(f"  Samples: {total_samples}")


async def main():
    parser = argparse.ArgumentParser(
        description="Capture audio from default HA Voice PE firmware"
    )
    parser.add_argument("device_ip", help="IP address of the ESPHome device")
    parser.add_argument(
        "--port", type=int, default=6053, help="ESPHome native API port (default: 6053)"
    )
    parser.add_argument("--password", default="", help="API password if set")
    parser.add_argument("--output", "-o", default=None, help="Output WAV file path")
    parser.add_argument(
        "--noise-suppression", type=int, default=0, help="Noise suppression level (0-4)"
    )
    parser.add_argument(
        "--auto-gain", type=int, default=0, help="Auto gain in dBFS (0-31)"
    )
    parser.add_argument(
        "--volume-multiplier", type=float, default=1.0, help="Volume multiplier"
    )
    args = parser.parse_args()

    if args.output is None:
        args.output = f"capture_default_{datetime.now().strftime('%H%M%S')}.wav"

    capture = AudioCapture(args.output)

    print(f"Connecting to {args.device_ip}:{args.port}...")
    client = APIClient(args.device_ip, args.port, args.password)

    try:
        await client.connect(login=True)
        info = await client.device_info()
        print(f"Connected to: {info.name} (ESPHome {info.esphome_version})")

        print(f"\nSubscribing to voice assistant...")
        print(f"  Trigger the wake word or press the button to start capture.")
        print(f"  Press Ctrl+C to stop and save.\n")

        unsub = client.subscribe_voice_assistant(
            handle_start=capture.handle_start,
            handle_stop=capture.handle_stop,
            handle_audio=capture.handle_audio,
        )

        # Keep running until Ctrl+C
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nStopping...")
            unsub()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
    finally:
        capture.save_wav()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
