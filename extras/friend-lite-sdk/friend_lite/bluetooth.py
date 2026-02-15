import asyncio
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

from .uuids import OMI_AUDIO_CHAR_UUID, OMI_BUTTON_CHAR_UUID


def print_devices() -> None:
    devices = asyncio.run(BleakScanner.discover())
    for i, d in enumerate(devices):
        print(f"{i}. {d.name} [{d.address}]")


class WearableConnection:
    """Base class for BLE wearable device connections.

    Provides connect/disconnect lifecycle, audio subscription, and
    disconnect-wait primitives shared by all wearable devices.
    """

    def __init__(self, mac_address: str) -> None:
        self._mac_address = mac_address
        self._client: Optional[BleakClient] = None
        self._disconnected = asyncio.Event()

    async def __aenter__(self) -> "WearableConnection":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self._client is not None:
            return

        def _on_disconnect(_client: BleakClient) -> None:
            self._disconnected.set()

        self._client = BleakClient(
            self._mac_address,
            disconnected_callback=_on_disconnect,
        )
        await self._client.connect()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        await self._client.disconnect()
        self._client = None
        self._disconnected.set()

    async def subscribe_audio(self, callback: Callable[[int, bytearray], None]) -> None:
        await self.subscribe(OMI_AUDIO_CHAR_UUID, callback)

    async def subscribe(self, uuid: str, callback: Callable[[int, bytearray], None]) -> None:
        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.start_notify(uuid, callback)

    async def wait_until_disconnected(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._disconnected.wait()
        else:
            await asyncio.wait_for(self._disconnected.wait(), timeout=timeout)


class OmiConnection(WearableConnection):
    """OMI device with button support."""

    async def subscribe_button(self, callback: Callable[[int, bytearray], None]) -> None:
        await self.subscribe(OMI_BUTTON_CHAR_UUID, callback)


async def listen_to_omi(mac_address: str, char_uuid: str, data_handler) -> None:
    """Backward-compatible wrapper for older consumers."""
    async with OmiConnection(mac_address) as conn:
        await conn.subscribe(char_uuid, data_handler)
        await conn.wait_until_disconnected()
