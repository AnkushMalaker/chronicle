#!/usr/bin/env python3
"""
Mock Streaming STT Server - Deepgram-compatible WebSocket server for testing.

This server mimics Deepgram's streaming transcription API with nested JSON responses
that match the extraction paths used in the config (e.g., channel.alternatives[0].transcript).

Architecture:
- Async WebSocket server on 0.0.0.0:9999
- Sends interim results every 10 audio chunks
- Sends final results on CloseStream with >2s duration and >5 words (speech detection thresholds)
"""

import asyncio
import json
import logging
import argparse
from typing import Optional
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_deepgram_response(
    transcript: str,
    is_final: bool,
    words: Optional[list] = None,
    confidence: float = 0.99
) -> dict:
    """
    Create Deepgram-compatible nested response format.

    Format matches extraction paths in config:
    - channel.alternatives[0].transcript
    - channel.alternatives[0].words
    """
    if words is None:
        # Generate word timestamps from transcript
        words = []
        current_time = 0.0
        for word in transcript.split():
            words.append({
                "word": word,
                "start": current_time,
                "end": current_time + 0.3,
                "confidence": confidence
            })
            current_time += 0.35  # 0.3s word + 0.05s gap

    return {
        "type": "Results",
        "is_final": is_final,
        "channel": {
            "alternatives": [{
                "transcript": transcript,
                "confidence": confidence,
                "words": words
            }]
        }
    }


def create_final_response() -> dict:
    """
    Create final response with >2s duration and >5 words.

    Speech detection thresholds (docker-compose-test.yml):
    - SPEECH_DETECTION_MIN_DURATION: 2.0s
    - SPEECH_DETECTION_MIN_WORDS: 5
    """
    # Create 7 words spanning >2.0 seconds
    words = []
    transcript_words = ["This", "is", "a", "test", "conversation", "about", "hiking"]

    current_time = 0.0
    for word in transcript_words:
        words.append({
            "word": word,
            "start": current_time,
            "end": current_time + 0.35,
            "confidence": 0.99
        })
        current_time += 0.4  # 0.35s word + 0.05s gap

    # Final timestamp should be >2.0s
    assert words[-1]["end"] > 2.0, f"Duration {words[-1]['end']}s must be >2.0s"

    transcript = " ".join(transcript_words)

    return create_deepgram_response(
        transcript=transcript,
        is_final=True,
        words=words,
        confidence=0.99
    )


async def handle_client(websocket: WebSocketServerProtocol):
    """Handle WebSocket client connection."""
    client_id = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
    logger.info(f"Client connected: {client_id}")

    chunk_count = 0

    try:
        # Send initial empty result
        initial = create_deepgram_response(transcript="", is_final=False)
        await websocket.send(json.dumps(initial))
        logger.debug(f"Sent initial result to {client_id}")

        async for message in websocket:
            # Handle binary audio chunks
            if isinstance(message, bytes):
                chunk_count += 1
                logger.debug(f"Received audio chunk {chunk_count} from {client_id}")

                # Send interim results every 10 chunks
                if chunk_count % 10 == 0:
                    interim = create_deepgram_response(
                        transcript=f"Interim transcription chunk {chunk_count // 10}",
                        is_final=False
                    )
                    await websocket.send(json.dumps(interim))
                    logger.debug(f"Sent interim result to {client_id}")

            # Handle control messages
            elif isinstance(message, str):
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")

                    if msg_type == "CloseStream":
                        logger.info(f"Received CloseStream from {client_id}")

                        # Send final result with >2s duration and >5 words
                        final = create_final_response()
                        await websocket.send(json.dumps(final))
                        logger.info(f"Sent final result to {client_id}: {final['channel']['alternatives'][0]['transcript']}")

                        # Close connection gracefully
                        await websocket.close()
                        break

                    else:
                        logger.warning(f"Unknown message type from {client_id}: {msg_type}")

                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from {client_id}: {message}")

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Client disconnected: {client_id}")

    except Exception as e:
        logger.error(f"Error handling client {client_id}: {e}", exc_info=True)

    finally:
        logger.info(f"Connection closed: {client_id}, processed {chunk_count} chunks")


async def main(host: str, port: int):
    """Start WebSocket server."""
    logger.info(f"Starting Mock Streaming STT Server on {host}:{port}")
    logger.info(f"Deepgram-compatible nested response format")
    logger.info(f"Speech detection: >2.0s duration, >5 words")

    async with websockets.serve(handle_client, host, port):
        logger.info(f"Server ready and listening on ws://{host}:{port}")
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock Streaming STT Server")
    parser.add_argument("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9999, help="Server port (default: 9999)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
