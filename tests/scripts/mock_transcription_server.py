#!/usr/bin/env python3
"""
Mock WebSocket Transcription Server for Testing

This server mimics a streaming transcription service (like Deepgram) by accepting
WebSocket connections and returning mock transcription results.

Usage:
    python mock_transcription_server.py [--port PORT] [--host HOST]
"""
import asyncio
import json
import logging
from typing import Optional

try:
    import websockets
    from websockets.server import WebSocketServerProtocol, serve
except ImportError:
    print(
        "ERROR: websockets package not found. Install with: uv pip install websockets"
    )
    exit(1)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


class MockTranscriptionServer:
    """Mock transcription WebSocket server that returns predefined transcripts."""

    def __init__(self, host: str = "localhost", port: int = 9999):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.Server] = None

    async def handle_connection(self, websocket: WebSocketServerProtocol):
        """Handle a single WebSocket connection."""
        client_id = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        logger.info(f"📡 New connection from {client_id}")

        try:
            # Send initial connection acknowledgment (simplified format for mock config)
            await websocket.send(
                json.dumps(
                    {
                        "type": "Results",
                        "is_final": False,
                        "text": "",
                        "words": [],
                        "segments": [],
                    }
                )
            )

            chunk_count = 0
            duration = 0.0

            async for message in websocket:
                if isinstance(message, bytes):
                    # Audio chunk received
                    chunk_count += 1
                    duration += 0.1  # Assume 100ms chunks

                    # Every 10 chunks, send an interim result (simplified format)
                    if chunk_count % 10 == 0:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "Results",
                                    "is_final": False,
                                    "text": f"This is mock transcription chunk {chunk_count // 10}",
                                    "confidence": 0.95,
                                    "words": [
                                        {
                                            "word": "This",
                                            "start": 0.0,
                                            "end": 0.1,
                                            "confidence": 0.95,
                                        },
                                        {
                                            "word": "is",
                                            "start": 0.1,
                                            "end": 0.2,
                                            "confidence": 0.95,
                                        },
                                        {
                                            "word": "mock",
                                            "start": 0.2,
                                            "end": 0.3,
                                            "confidence": 0.95,
                                        },
                                        {
                                            "word": "transcription",
                                            "start": 0.3,
                                            "end": 0.5,
                                            "confidence": 0.95,
                                        },
                                    ],
                                    "segments": [],
                                }
                            )
                        )

                    # Every 50 chunks, send a final result (simplified format)
                    # Make sure words span at least 2 seconds for speech detection
                    if chunk_count % 50 == 0:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "Results",
                                    "is_final": True,
                                    "text": "This is a final mock transcription segment.",
                                    "confidence": 0.98,
                                    "words": [
                                        {
                                            "word": "This",
                                            "start": 0.0,
                                            "end": 0.3,
                                            "confidence": 0.98,
                                        },
                                        {
                                            "word": "is",
                                            "start": 0.3,
                                            "end": 0.6,
                                            "confidence": 0.98,
                                        },
                                        {
                                            "word": "a",
                                            "start": 0.6,
                                            "end": 0.8,
                                            "confidence": 0.98,
                                        },
                                        {
                                            "word": "final",
                                            "start": 0.8,
                                            "end": 1.2,
                                            "confidence": 0.98,
                                        },
                                        {
                                            "word": "mock",
                                            "start": 1.2,
                                            "end": 1.5,
                                            "confidence": 0.98,
                                        },
                                        {
                                            "word": "transcription",
                                            "start": 1.5,
                                            "end": 2.0,
                                            "confidence": 0.98,
                                        },
                                        {
                                            "word": "segment",
                                            "start": 2.0,
                                            "end": 2.5,
                                            "confidence": 0.98,
                                        },
                                    ],
                                    "segments": [],
                                }
                            )
                        )

                elif isinstance(message, str):
                    # Control message received
                    try:
                        msg = json.loads(message)
                        if msg.get("type") == "CloseStream":
                            logger.info(f"📴 Received CloseStream from {client_id}")
                            # Send final results (simplified format) with words spanning >2s
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "Results",
                                        "is_final": True,
                                        "text": "This is the complete mock transcription result.",
                                        "confidence": 0.98,
                                        "words": [
                                            {
                                                "word": "This",
                                                "start": 0.0,
                                                "end": 0.4,
                                                "confidence": 0.98,
                                            },
                                            {
                                                "word": "is",
                                                "start": 0.4,
                                                "end": 0.7,
                                                "confidence": 0.98,
                                            },
                                            {
                                                "word": "the",
                                                "start": 0.7,
                                                "end": 1.0,
                                                "confidence": 0.98,
                                            },
                                            {
                                                "word": "complete",
                                                "start": 1.0,
                                                "end": 1.5,
                                                "confidence": 0.98,
                                            },
                                            {
                                                "word": "mock",
                                                "start": 1.5,
                                                "end": 1.9,
                                                "confidence": 0.98,
                                            },
                                            {
                                                "word": "transcription",
                                                "start": 1.9,
                                                "end": 2.5,
                                                "confidence": 0.98,
                                            },
                                            {
                                                "word": "result",
                                                "start": 2.5,
                                                "end": 3.0,
                                                "confidence": 0.98,
                                            },
                                        ],
                                        "segments": [],
                                    }
                                )
                            )
                            break
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ Invalid JSON from {client_id}: {message}")

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"🔌 Connection closed by {client_id}")
        except Exception as e:
            logger.error(f"❌ Error handling connection from {client_id}: {e}")
        finally:
            logger.info(
                f"✅ Cleaned up connection from {client_id} (received {chunk_count} chunks)"
            )

    async def start(self):
        """Start the WebSocket server."""
        logger.info(f"🚀 Starting Mock Transcription Server on {self.host}:{self.port}")

        self.server = await serve(self.handle_connection, self.host, self.port)

        logger.info(
            f"✅ Mock Transcription Server ready at ws://{self.host}:{self.port}"
        )

    async def run_forever(self):
        """Start the server and run until interrupted."""
        await self.start()

        # Keep server running
        await asyncio.Future()  # Run forever

    async def stop(self):
        """Stop the WebSocket server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("🛑 Mock Transcription Server stopped")


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Mock WebSocket Transcription Server")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=9999, help="Port to bind to (default: 9999)"
    )
    args = parser.parse_args()

    server = MockTranscriptionServer(host=args.host, port=args.port)

    try:
        await server.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️ Keyboard interrupt received")
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
