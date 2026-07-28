"""TCP server that receives stored audio data from an OMI device over WiFi."""

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# SD_BLE_SIZE (440) * 10 — matches firmware chunk size
READ_SIZE = 4400


class WifiAudioReceiver:
    """Async TCP server that listens for the device's TCP connection.

    The OMI device acts as a TCP *client* and connects to this server
    to stream stored audio data from its SD card.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 12345,
        on_data: Callable[[bytes], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_data = on_data
        self._server: asyncio.Server | None = None
        self.bytes_received: int = 0
        self.connected = asyncio.Event()
        self.finished = asyncio.Event()

    async def start(self) -> None:
        """Start TCP server and wait for device connection."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        addrs = [s.getsockname() for s in self._server.sockets]
        logger.info("TCP server listening on %s", addrs)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming TCP connection from device."""
        peer = writer.get_extra_info("peername")
        logger.info("Device connected from %s", peer)
        self.connected.set()

        try:
            while True:
                data = await reader.read(READ_SIZE)
                if not data:
                    break
                self.bytes_received += len(data)
                if self.on_data:
                    self.on_data(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("TCP receive error: %s", e)
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info(
                "Device disconnected. Total received: %d bytes", self.bytes_received
            )
            self.finished.set()

    async def stop(self) -> None:
        """Stop the TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
