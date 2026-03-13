"""
Test script for playing audio through the HA Voice Preview Edition device.

This script connects to the VA-PE device via the ESPHome native API and
commands its media_player to play audio from an HTTP URL.

Prerequisites:
  - Device must be running firmware with media_player + speaker output
    (the official HA Voice PE firmware, or voice-chronicle.yaml)
  - Device must be on the same network
  - aioesphomeapi must be installed: uv run --group test python test_audio_output.py

Usage:
  # Play a generated test tone
  uv run --group test python test_audio_output.py --device-ip 192.168.0.XXX -v

  # Play a specific WAV file
  uv run --group test python test_audio_output.py --device-ip 192.168.0.XXX --file my_audio.wav

  # Just list entities on the device (diagnostic)
  uv run --group test python test_audio_output.py --device-ip 192.168.0.XXX --list-entities
"""

import argparse
import asyncio
import io
import logging
import math
import socket
import struct
import threading
import wave
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import aioesphomeapi

logger = logging.getLogger(__name__)


def get_local_ip() -> str:
    """Get the local IP address that's routable to the device's network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def generate_test_tone(
    frequency: float = 440.0,
    duration: float = 3.0,
    sample_rate: int = 48000,
    amplitude: float = 0.5,
) -> bytes:
    """Generate a sine wave test tone as WAV bytes."""
    num_samples = int(sample_rate * duration)
    buf = io.BytesIO()

    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)

        frames = bytearray()
        for i in range(num_samples):
            t = i / sample_rate
            envelope = 1.0
            fade_samples = int(0.05 * sample_rate)
            if i < fade_samples:
                envelope = i / fade_samples
            elif i > num_samples - fade_samples:
                envelope = (num_samples - i) / fade_samples

            sample = amplitude * envelope * math.sin(2.0 * math.pi * frequency * t)
            frames.extend(struct.pack("<h", int(sample * 32767)))

        wav.writeframes(bytes(frames))

    return buf.getvalue()


class AudioHTTPHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves a single audio file."""

    audio_data: bytes = b""
    content_type: str = "audio/wav"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.audio_data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(self.audio_data)

    def log_message(self, format, *args):
        logger.info("HTTP: %s", format % args)


def start_http_server(audio_data: bytes, port: int = 8080) -> HTTPServer:
    """Start an HTTP server serving the audio data in a background thread."""
    AudioHTTPHandler.audio_data = audio_data

    server = HTTPServer(("0.0.0.0", port), AudioHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("HTTP server started on port %d", port)
    return server


async def connect_to_device(
    device_ip: str,
    port: int = 6053,
    password: str = "",
    noise_psk: str | None = None,
) -> aioesphomeapi.APIClient:
    """Connect to ESPHome device via native API."""
    client = aioesphomeapi.APIClient(
        address=device_ip,
        port=port,
        password=password,
        noise_psk=noise_psk,
    )

    logger.info("Connecting to device at %s:%d ...", device_ip, port)
    await client.connect(login=True)

    device_info = await client.device_info()
    logger.info(
        "Connected to: %s (%s) running ESPHome %s",
        device_info.friendly_name or device_info.name,
        device_info.mac_address,
        device_info.esphome_version,
    )

    return client


async def list_entities(client: aioesphomeapi.APIClient) -> None:
    """List all entities on the device."""
    entities, services = await client.list_entities_services()

    print("\n=== Device Entities ===")
    for entity in entities:
        entity_type = type(entity).__name__.replace("Info", "")
        print(f"  [{entity_type}] {entity.name or entity.object_id} (key={entity.key})")

    print(f"\n=== Services ({len(services)}) ===")
    for service in services:
        print(f"  {service.name}")

    media_players = [
        e for e in entities if isinstance(e, aioesphomeapi.MediaPlayerInfo)
    ]
    if media_players:
        print(f"\nFound {len(media_players)} media player(s):")
        for mp in media_players:
            print(f"  - {mp.name or mp.object_id} (key={mp.key})")
    else:
        print(
            "\nNo media players found! Device needs firmware with media_player component."
        )

    return entities


async def play_audio(
    client: aioesphomeapi.APIClient,
    audio_url: str,
    announcement: bool = True,
) -> None:
    """Play audio via the device's media_player."""
    entities, _ = await client.list_entities_services()

    media_players = [
        e for e in entities if isinstance(e, aioesphomeapi.MediaPlayerInfo)
    ]

    if not media_players:
        raise RuntimeError(
            "No media_player entities found on device. "
            "Ensure firmware has media_player + speaker components configured."
        )

    target = None
    for mp in media_players:
        name = (mp.name or mp.object_id).lower()
        if "group" not in name and "sendspin" not in name:
            target = mp
            break
    if target is None:
        target = media_players[0]

    logger.info(
        "Playing audio via '%s' (key=%d): %s",
        target.name or target.object_id,
        target.key,
        audio_url,
    )

    client.media_player_command(
        key=target.key,
        media_url=audio_url,
        announcement=announcement,
    )

    logger.info("Play command sent. Audio should be playing on the device.")


async def main(args: argparse.Namespace) -> None:
    """Main entry point."""
    client = await connect_to_device(
        device_ip=args.device_ip,
        port=args.port,
        password=args.password,
        noise_psk=args.noise_psk,
    )

    try:
        if args.list_entities:
            await list_entities(client)
            return

        if args.file:
            audio_path = Path(args.file)
            if not audio_path.exists():
                raise FileNotFoundError(f"Audio file not found: {audio_path}")
            audio_data = audio_path.read_bytes()
            logger.info("Loaded audio file: %s (%d bytes)", audio_path, len(audio_data))
        else:
            logger.info(
                "Generating test tone: %dHz, %.1fs duration, %dHz sample rate",
                args.frequency,
                args.duration,
                args.sample_rate,
            )
            audio_data = generate_test_tone(
                frequency=args.frequency,
                duration=args.duration,
                sample_rate=args.sample_rate,
            )
            logger.info("Generated test tone: %d bytes", len(audio_data))

        local_ip = get_local_ip()
        http_server = start_http_server(audio_data, port=args.http_port)

        audio_url = f"http://{local_ip}:{args.http_port}/audio.wav"
        logger.info("Audio URL: %s", audio_url)

        await play_audio(client, audio_url, announcement=not args.media)

        logger.info("Waiting %d seconds for playback to finish...", args.wait)
        await asyncio.sleep(args.wait)

        http_server.shutdown()

    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Play audio through HA Voice Preview Edition device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Play test tone (440Hz A note)
  uv run --group test python test_audio_output.py --device-ip 192.168.0.100 -v

  # Play a WAV file
  uv run --group test python test_audio_output.py --device-ip 192.168.0.100 --file my_audio.wav

  # List device entities (diagnostic)
  uv run --group test python test_audio_output.py --device-ip 192.168.0.100 --list-entities
        """,
    )

    parser.add_argument(
        "--device-ip", required=True, help="IP address of the VA-PE device"
    )
    parser.add_argument(
        "--port", type=int, default=6053, help="ESPHome native API port (default: 6053)"
    )
    parser.add_argument(
        "--password", default="", help="API password (if set in firmware)"
    )
    parser.add_argument(
        "--noise-psk",
        default=None,
        help="API encryption key (base64-encoded noise PSK)",
    )

    parser.add_argument(
        "--file", help="Path to WAV file to play (default: generate test tone)"
    )
    parser.add_argument(
        "--frequency",
        type=float,
        default=440.0,
        help="Test tone frequency in Hz (default: 440)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=3.0,
        help="Test tone duration in seconds (default: 3)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        help="Test tone sample rate (default: 48000)",
    )

    parser.add_argument(
        "--media", action="store_true", help="Play as media instead of announcement"
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8080,
        help="Port for temporary HTTP audio server (default: 8080)",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=10,
        help="Seconds to wait for playback to finish (default: 10)",
    )

    parser.add_argument(
        "--list-entities",
        action="store_true",
        help="List all entities on the device and exit",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )

    args = parser.parse_args()

    log_level = logging.WARNING
    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose >= 1:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(main(args))
