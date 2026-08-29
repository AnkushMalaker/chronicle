from types import SimpleNamespace
from unittest.mock import AsyncMock

import opuslib
import pytest
from audio_contract.v2 import audio_pb2
from audio_v2_adapter import PcmToOpus, forward_device_events


def test_pcm_adapter_emits_exactly_one_decodable_twenty_ms_packet():
    encoder = PcmToOpus(rate=16_000, width=2, channels=1)
    assert encoder.push(b"\x00\x00" * 160) == []
    packets = encoder.push(b"\x00\x00" * 160)
    assert len(packets) == 1
    assert len(opuslib.Decoder(16_000, 1).decode(packets[0], 320)) == 640


@pytest.mark.asyncio
async def test_button_event_crosses_generated_v2_control_only():
    device = SimpleNamespace(
        get_event=AsyncMock(
            side_effect=[
                {"type": "dial-event", "direction": "CLOCKWISE"},
                {"type": "button-event", "state": "SINGLE_PRESS"},
                RuntimeError("done"),
            ]
        )
    )
    client = SimpleNamespace(send_button=AsyncMock())
    try:
        await forward_device_events(device, client)
    except RuntimeError as error:
        assert str(error) == "done"
    client.send_button.assert_awaited_once_with(audio_pb2.BUTTON_STATE_SINGLE_PRESS)
