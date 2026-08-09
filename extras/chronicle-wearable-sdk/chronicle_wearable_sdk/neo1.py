from .bluetooth import WearableConnection
from .uuids import NEO1_CTRL_CHAR_UUID


class Neo1Connection(WearableConnection):
    """Neo1 device with sleep/wake control (no buttons)."""

    async def sleep(self) -> None:
        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.write_gatt_char(NEO1_CTRL_CHAR_UUID, b"\x00", response=True)

    async def wake(self) -> None:
        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.write_gatt_char(NEO1_CTRL_CHAR_UUID, b"\x01", response=True)

    async def disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            try:
                await self.sleep()
            except Exception:
                pass  # best-effort sleep before disconnect
        await super().disconnect()
