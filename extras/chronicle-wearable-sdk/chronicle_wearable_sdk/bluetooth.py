import asyncio
import uuid
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

from .uuids import (
    BATTERY_LEVEL_CHAR_UUID,
    ELATO_SPEAKER_CHAR_UUID,
    ELATO_SPEAKER_STATUS_CHAR_UUID,
    FEATURE_HAPTIC,
    FEATURE_WIFI,
    FEATURES_CHAR_UUID,
    HAPTIC_CHAR_UUID,
    OMI_AUDIO_CHAR_UUID,
    OMI_BUTTON_CHAR_UUID,
    SPEAKER_OP_AUDIO,
    SPEAKER_OP_END,
    SPEAKER_OP_START,
    SPEAKER_OP_STOP,
    SPEAKER_STATUS_CANCELLED,
    SPEAKER_STATUS_DONE,
    SPEAKER_STATUS_FAILED,
    SPEAKER_STATUS_STARTED,
    STORAGE_DATA_STREAM_CHAR_UUID,
    STORAGE_READ_CONTROL_CHAR_UUID,
    STORAGE_WIFI_CHAR_UUID,
)


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

    async def read_battery_level(self) -> int:
        """Read the current battery level (0-100). Returns -1 on failure."""
        if self._client is None:
            raise RuntimeError("Not connected to device")
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID)
            if data:
                return data[0]
        except Exception:
            pass
        return -1

    async def subscribe_battery(self, callback: Callable[[int], None]) -> None:
        """Subscribe to battery level notifications.

        *callback* receives a single int (0-100) each time the device
        reports an updated level.
        """

        def _on_notify(_sender: int, data: bytearray) -> None:
            if data:
                callback(data[0])

        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.start_notify(BATTERY_LEVEL_CHAR_UUID, _on_notify)

    async def subscribe_audio(self, callback: Callable[[int, bytearray], None]) -> None:
        await self.subscribe(OMI_AUDIO_CHAR_UUID, callback)

    async def subscribe(
        self, uuid: str, callback: Callable[[int, bytearray], None]
    ) -> None:
        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.start_notify(uuid, callback)

    async def wait_until_disconnected(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._disconnected.wait()
        else:
            await asyncio.wait_for(self._disconnected.wait(), timeout=timeout)


class OmiConnection(WearableConnection):
    """OMI device with button and WiFi sync support."""

    async def subscribe_button(
        self, callback: Callable[[int, bytearray], None]
    ) -> None:
        await self.subscribe(OMI_BUTTON_CHAR_UUID, callback)

    # -- Speaker downlink (Elato) ------------------------------------------

    def _speaker_chunk_size(self) -> int:
        """Max Opus bytes per BLE write, derived from the negotiated MTU.

        Each write carries a 2-byte header (opcode + flags) on top of the 3-byte
        ATT write overhead. Fall back to a tiny but safe payload if the MTU is the
        23-byte default (the firmware reassembles fragments, so any size works).
        """
        mtu = getattr(self._client, "mtu_size", 0) or 0
        return max(mtu - 3 - 2, 18)

    def supports_speaker_protocol_v1(self) -> bool:
        """Whether firmware exposes bound playback-status notifications."""
        if self._client is None:
            raise RuntimeError("Not connected to device")
        return (
            self._client.services.get_characteristic(ELATO_SPEAKER_STATUS_CHAR_UUID)
            is not None
        )

    @staticmethod
    def _speaker_binding(response_id: str, generation: int) -> bytes:
        if generation < 0:
            raise ValueError("generation must be non-negative")
        return uuid.UUID(response_id).bytes + generation.to_bytes(8, "little")

    async def _speaker_control(
        self, opcode: int, response_id: str, generation: int
    ) -> None:
        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.write_gatt_char(
            ELATO_SPEAKER_CHAR_UUID,
            bytes([opcode]) + self._speaker_binding(response_id, generation),
            response=True,
        )

    async def speaker_start(self, response_id: str, generation: int) -> None:
        """Begin one bound speaker clip and reset the playback ring."""
        await self._speaker_control(SPEAKER_OP_START, response_id, generation)

    async def speaker_end(self, response_id: str, generation: int) -> None:
        """End a bound clip; firmware drains then reports ``done``."""
        await self._speaker_control(SPEAKER_OP_END, response_id, generation)

    async def speaker_stop(
        self, response_id: str, cancellation_generation: int
    ) -> None:
        """Stop a bound response when the cancellation generation is current."""
        await self._speaker_control(
            SPEAKER_OP_STOP, response_id, cancellation_generation
        )

    async def subscribe_speaker_status(
        self, callback: Callable[[str, int, str], None]
    ) -> None:
        """Subscribe to firmware-confirmed playback state for protocol v1.

        Notifications are ``status | response UUID | response generation``.  A
        cancellation notification carries the original response generation; the
        newer cancellation generation is used only by firmware's stale-command gate.
        """
        if self._client is None:
            raise RuntimeError("Not connected to device")
        states = {
            SPEAKER_STATUS_STARTED: "started",
            SPEAKER_STATUS_DONE: "done",
            SPEAKER_STATUS_CANCELLED: "cancelled",
            SPEAKER_STATUS_FAILED: "failed",
        }

        def _on_notify(_sender: int, data: bytearray) -> None:
            if len(data) != 25:
                raise ValueError("invalid Elato speaker status length")
            state = states.get(data[0])
            if state is None:
                raise ValueError("invalid Elato speaker status opcode")
            response_id = str(uuid.UUID(bytes=bytes(data[1:17])))
            generation = int.from_bytes(data[17:25], "little")
            callback(response_id, generation, state)

        await self._client.start_notify(ELATO_SPEAKER_STATUS_CHAR_UUID, _on_notify)

    async def write_speaker_audio(self, opus_packet: bytes) -> None:
        """Write one Opus packet (24 kHz mono) to the speaker, fragmented to the MTU.

        Each fragment is ``[SPEAKER_OP_AUDIO][flags][opus bytes]``; the final fragment
        of a packet sets flags bit0 so the firmware decodes the reassembled packet.
        """
        if self._client is None:
            raise RuntimeError("Not connected to device")
        size = self._speaker_chunk_size()
        n = len(opus_packet)
        offset = 0
        while offset < n:
            chunk = opus_packet[offset : offset + size]
            offset += len(chunk)
            flags = 0x01 if offset >= n else 0x00  # bit0 = final fragment
            await self._client.write_gatt_char(
                ELATO_SPEAKER_CHAR_UUID,
                bytes([SPEAKER_OP_AUDIO, flags]) + chunk,
                response=False,
            )

    # -- Haptic ------------------------------------------------------------

    async def play_haptic(self, pattern: int = 1) -> None:
        """Trigger the haptic motor.

        *pattern*: 1 = short (100ms), 2 = medium (300ms), 3 = long (500ms).
        """
        if self._client is None:
            raise RuntimeError("Not connected to device")
        if pattern not in (1, 2, 3):
            raise ValueError("pattern must be 1 (100ms), 2 (300ms), or 3 (500ms)")
        await self._client.write_gatt_char(
            HAPTIC_CHAR_UUID, bytes([pattern]), response=True
        )

    async def is_haptic_supported(self) -> bool:
        """Check whether the device has a haptic motor."""
        features = await self.read_features()
        return bool(features & FEATURE_HAPTIC)

    # -- Features ----------------------------------------------------------

    async def read_features(self) -> int:
        """Read device feature bitmask."""
        if self._client is None:
            raise RuntimeError("Not connected to device")
        data = await self._client.read_gatt_char(FEATURES_CHAR_UUID)
        return int.from_bytes(data, byteorder="little")

    async def is_wifi_supported(self) -> bool:
        """Check whether the device supports WiFi sync."""
        features = await self.read_features()
        return bool(features & FEATURE_WIFI)

    # -- WiFi sync ---------------------------------------------------------

    async def _wifi_command(self, payload: bytes, timeout: float = 5.0) -> int:
        """Send a command to the WiFi characteristic and wait for notify response.

        Returns the response byte (0 = success).
        """
        if self._client is None:
            raise RuntimeError("Not connected to device")

        response_event = asyncio.Event()
        response_value: list[int] = []

        def _on_notify(_sender: int, data: bytearray) -> None:
            if data:
                response_value.append(data[0])
            response_event.set()

        await self._client.start_notify(STORAGE_WIFI_CHAR_UUID, _on_notify)
        try:
            await self._client.write_gatt_char(
                STORAGE_WIFI_CHAR_UUID, payload, response=True
            )
            await asyncio.wait_for(response_event.wait(), timeout=timeout)
        finally:
            await self._client.stop_notify(STORAGE_WIFI_CHAR_UUID)

        return response_value[0] if response_value else -1

    async def setup_wifi(self, ssid: str, password: str) -> int:
        """Send WiFi AP credentials to device. Returns response code (0=success)."""
        ssid_bytes = ssid.encode("utf-8")
        pwd_bytes = password.encode("utf-8")
        payload = (
            bytes([0x01, len(ssid_bytes)])
            + ssid_bytes
            + bytes([len(pwd_bytes)])
            + pwd_bytes
        )
        return await self._wifi_command(payload)

    async def start_wifi(self) -> int:
        """Send WIFI_START command. Returns response code (0=success)."""
        return await self._wifi_command(bytes([0x02]))

    async def stop_wifi(self) -> int:
        """Send WIFI_SHUTDOWN command. Returns response code (0=success)."""
        return await self._wifi_command(bytes([0x03]))

    # -- Storage -----------------------------------------------------------

    async def get_storage_info(self) -> tuple[int, int]:
        """Read (file_size, offset) from storage read control characteristic."""
        if self._client is None:
            raise RuntimeError("Not connected to device")
        data = await self._client.read_gatt_char(STORAGE_READ_CONTROL_CHAR_UUID)
        file_size = int.from_bytes(data[0:4], byteorder="little")
        offset = int.from_bytes(data[4:8], byteorder="little")
        return (file_size, offset)

    async def start_storage_read(self, file_num: int = 0, offset: int = 0) -> None:
        """Send READ command to trigger data transfer.

        Written to the data stream characteristic (0x30295781) which is
        both the command write target and the data notification source.
        Firmware expects: [command=0, file_num, offset(4 bytes big-endian)]
        """
        if self._client is None:
            raise RuntimeError("Not connected to device")
        payload = bytes([0x00, file_num]) + offset.to_bytes(4, byteorder="big")
        await self._client.write_gatt_char(
            STORAGE_DATA_STREAM_CHAR_UUID, payload, response=True
        )

    async def subscribe_storage_data(
        self, callback: Callable[[int, bytearray], None]
    ) -> None:
        """Subscribe to storage data stream notifications (for BLE storage reads)."""
        if self._client is None:
            raise RuntimeError("Not connected to device")
        await self._client.start_notify(STORAGE_DATA_STREAM_CHAR_UUID, callback)


async def listen_to_omi(mac_address: str, char_uuid: str, data_handler) -> None:
    """Backward-compatible wrapper for older consumers."""
    async with OmiConnection(mac_address) as conn:
        await conn.subscribe(char_uuid, data_handler)
        await conn.wait_until_disconnected()
