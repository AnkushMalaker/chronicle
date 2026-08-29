#!/usr/bin/env python3
"""
HAVPE Relay - Bidirectional TCP/ESPHome→WebSocket bridge for Chronicle.

Two connections to the device:
  TCP (:8989)         — audio streaming (device → relay → backend)
  ESPHome API (:6053) — button/dial events (device → backend),
                        LED control + audio playback (backend → device)

The firmware's device-local JSONL/PCM stream is adapted to Chronicle audio v2.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import wave

from chronicle_client import acheck_credentials
from dotenv import load_dotenv
from relay_core import RelayConfig, run_device_session
from service import install, kickstart, logs, status, uninstall

load_dotenv()
logger = logging.getLogger(__name__)


def _make_wav_callbacks(dump_dir: str):
    """Build on_audio_chunk / on_audio_event closures for WAV dumping."""
    wav_state = {
        "wav_file": None,
        "chunk_count": 0,
        "total_bytes": 0,
        "t_start": None,
        "rate": 16000,
        "width": 2,
        "channels": 1,
    }

    def _open_wav(rate: int, width: int, channels: int) -> wave.Wave_write:
        os.makedirs(dump_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(dump_dir, f"havpe_debug_{ts}.wav")
        wf = wave.open(path, "wb")
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        logger.info(
            "Debug audio dump: %s (rate=%d width=%d ch=%d)", path, rate, width, channels
        )
        return wf

    def on_audio_chunk(payload: bytes, length: int) -> None:
        s = wav_state
        if s["wav_file"]:
            s["wav_file"].writeframes(payload)
        s["chunk_count"] += 1
        s["total_bytes"] += length
        if s["t_start"] is None:
            s["t_start"] = time.monotonic()
        if s["chunk_count"] % 100 == 0:
            elapsed = time.monotonic() - s["t_start"]
            byte_rate = s["total_bytes"] / elapsed if elapsed > 0 else 0
            expected_rate = s["rate"] * s["width"] * s["channels"]
            ratio = byte_rate / expected_rate if expected_rate > 0 else 0
            logger.info(
                "Audio stats: %d chunks, %d bytes, %.1fs elapsed, "
                "%.0f B/s actual vs %d B/s declared (ratio=%.2fx)",
                s["chunk_count"],
                s["total_bytes"],
                elapsed,
                byte_rate,
                expected_rate,
                ratio,
            )

    def on_audio_event(msg_type: str, header: dict) -> None:
        s = wav_state
        if msg_type == "audio-start":
            d = header.get("data", {})
            s["rate"] = d.get("rate", 16000)
            s["width"] = d.get("width", 2)
            s["channels"] = d.get("channels", 1)
            s["wav_file"] = _open_wav(s["rate"], s["width"], s["channels"])
            s["chunk_count"] = 0
            s["total_bytes"] = 0
            s["t_start"] = time.monotonic()

        elif msg_type == "audio-stop":
            if s["t_start"]:
                elapsed = time.monotonic() - s["t_start"]
                byte_rate = s["total_bytes"] / elapsed if elapsed > 0 else 0
                expected_rate = s["rate"] * s["width"] * s["channels"]
                ratio = byte_rate / expected_rate if expected_rate > 0 else 0
                declared_duration = (
                    s["total_bytes"] / expected_rate if expected_rate > 0 else 0
                )
                logger.info(
                    "Session end: %d chunks, %d bytes in %.1fs wall-clock | "
                    "%.0f B/s actual vs %d B/s expected (ratio=%.2fx) | "
                    "declared duration=%.1fs",
                    s["chunk_count"],
                    s["total_bytes"],
                    elapsed,
                    byte_rate,
                    expected_rate,
                    ratio,
                    declared_duration,
                )
            if s["wav_file"]:
                s["wav_file"].close()
                logger.info("Debug WAV closed (%d bytes written)", s["total_bytes"])
                s["wav_file"] = None

    return on_audio_chunk, on_audio_event


async def main():
    parser = argparse.ArgumentParser(
        description="HAVPE Relay - Bidirectional Chronicle audio-v2 bridge"
    )
    parser.add_argument("--port", type=int, default=8989)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument(
        "--backend-url",
        type=str,
        default=os.getenv("BACKEND_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--backend-ws-url",
        type=str,
        default=os.getenv("BACKEND_WS_URL", "ws://localhost:8000"),
    )
    parser.add_argument("--api-key", type=str, default=os.getenv("CHRONICLE_API_KEY"))
    parser.add_argument(
        "--device-name", type=str, default=os.getenv("DEVICE_NAME", "havpe")
    )
    parser.add_argument(
        "--esphome-device-ip",
        type=str,
        default=os.getenv("ESPHOME_DEVICE_IP", ""),
        help="Static ESPHome device IP (default: use TCP client IP)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument(
        "--dump-audio",
        metavar="DIR",
        default=None,
        help="Dump raw device audio to WAV files in DIR (for debugging sample-rate mismatches)",
    )
    args = parser.parse_args()

    config = RelayConfig(
        backend_url=args.backend_url,
        backend_ws_url=args.backend_ws_url,
        api_key=args.api_key or "",
        device_name=args.device_name,
        esphome_device_ip=args.esphome_device_ip or "",
    )

    level = logging.WARNING - (10 * min(args.verbose, 2))
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=level)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)

    if not config.api_key:
        logger.error("Set CHRONICLE_API_KEY (env or --api-key)")
        return

    if not await acheck_credentials(config.api_key, config.backend_url):
        logger.error("Startup auth check failed")
        return

    dump_dir = args.dump_audio
    if dump_dir:
        logger.info("Audio dump enabled → %s", dump_dir)

    on_chunk = None
    on_event = None
    if dump_dir:
        on_chunk, on_event = _make_wav_callbacks(dump_dir)

    server = await asyncio.start_server(
        lambda r, w: run_device_session(
            r,
            w,
            config,
            on_audio_chunk=on_chunk,
            on_audio_event=on_event,
        ),
        args.host,
        args.port,
    )
    logger.info("Relay listening on %s:%d (TCP)", args.host, args.port)
    logger.info("Backend: %s", config.backend_url)

    async with server:
        await server.serve_forever()


def cli():
    """Entry point with subcommands for service management."""
    parser = argparse.ArgumentParser(description="HAVPE Relay")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="Launch menu bar app (default)")
    sub.add_parser("relay", help="Run CLI relay (no menu bar)")
    sub.add_parser("install", help="Install as macOS login service")
    sub.add_parser("uninstall", help="Remove macOS login service")
    sub.add_parser("kickstart", help="Relaunch the menu bar app")
    sub.add_parser("status", help="Show service status")
    sub.add_parser("logs", help="Tail service logs")

    args = parser.parse_args()
    command = args.command or "menu"

    if command == "menu":
        # Lazy import: macOS-only (rumps, darwin-only per pyproject.toml)
        from menu_relay import main as menu_main

        menu_main()
    elif command == "relay":
        sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip subcommand
        asyncio.run(main())
    elif command == "install":
        install()
    elif command == "uninstall":
        uninstall()
    elif command == "kickstart":
        kickstart()
    elif command == "status":
        status()
    elif command == "logs":
        logs()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in (
        "menu",
        "relay",
        "install",
        "uninstall",
        "kickstart",
        "status",
        "logs",
    ):
        cli()
    else:
        asyncio.run(main())
