import asyncio
import io
import uuid
import wave

from chronicle_wearable_sdk.bluetooth import OmiConnection
from chronicle_wearable_sdk.speaker_audio import encode_wav_to_opus_packets
from chronicle_wearable_sdk.uuids import (
    ELATO_SPEAKER_CHAR_UUID,
    ELATO_SPEAKER_STATUS_CHAR_UUID,
    SPEAKER_OP_START,
    SPEAKER_STATUS_STARTED,
)


class FakeServices:
    def __init__(self, supported: bool) -> None:
        self.supported = supported

    def get_characteristic(self, value: str):
        if self.supported and value == ELATO_SPEAKER_STATUS_CHAR_UUID:
            return object()
        return None


class FakeBleakClient:
    def __init__(self, *, status_supported: bool = True) -> None:
        self.services = FakeServices(status_supported)
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notifications = {}

    async def write_gatt_char(self, characteristic, value, *, response):
        self.writes.append((characteristic, value, response))

    async def start_notify(self, characteristic, callback):
        self.notifications[characteristic] = callback


def test_speaker_start_carries_response_binding_and_generation():
    async def scenario():
        connection = OmiConnection("00:11:22:33:44:55")
        client = FakeBleakClient()
        connection._client = client
        response_id = "00000000-0000-4000-8000-000000000123"

        await connection.speaker_start(response_id, 9)

        characteristic, payload, response = client.writes[0]
        assert characteristic == ELATO_SPEAKER_CHAR_UUID
        assert payload == (
            bytes([SPEAKER_OP_START])
            + uuid.UUID(response_id).bytes
            + (9).to_bytes(8, "little")
        )
        assert response is True

    asyncio.run(scenario())


def test_status_notification_returns_the_original_response_binding():
    async def scenario():
        connection = OmiConnection("00:11:22:33:44:55")
        client = FakeBleakClient()
        connection._client = client
        observed = []
        response_id = "00000000-0000-4000-8000-000000000123"
        await connection.subscribe_speaker_status(
            lambda rid, generation, state: observed.append((rid, generation, state))
        )

        callback = client.notifications[ELATO_SPEAKER_STATUS_CHAR_UUID]
        callback(
            1,
            bytearray(
                bytes([SPEAKER_STATUS_STARTED])
                + uuid.UUID(response_id).bytes
                + (8).to_bytes(8, "little")
            ),
        )

        assert observed == [(response_id, 8, "started")]
        assert connection.supports_speaker_protocol_v1() is True

    asyncio.run(scenario())


def test_old_firmware_without_status_characteristic_is_not_v1_capable():
    connection = OmiConnection("00:11:22:33:44:55")
    connection._client = FakeBleakClient(status_supported=False)

    assert connection.supports_speaker_protocol_v1() is False


def test_wav_response_is_resampled_and_packetized_for_elato():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x00" * 1_600)

    packets = encode_wav_to_opus_packets(buffer.getvalue())

    assert len(packets) == 2
    assert all(isinstance(packet, bytes) and packet for packet in packets)
