"""Chronicle audio-v2 adapter for HAVPE's device-local JSONL/PCM stream."""

from __future__ import annotations

import asyncio
import audioop
import io
import json
import logging
import time
import wave
from collections.abc import Callable

import opuslib
from audio_contract.v2 import audio_pb2
from chronicle_client.audio_v2 import AudioV2Client
from tone_server import serve_audio_bytes

logger = logging.getLogger(__name__)


class PcmToOpus:
    """Normalize arbitrary HAVPE PCM chunks into 16 kHz mono 20 ms Opus."""

    def __init__(self, *, rate: int, width: int, channels: int) -> None:
        if rate <= 0 or width not in {1, 2, 3, 4} or channels not in {1, 2}:
            raise ValueError("unsupported HAVPE PCM format")
        self.rate = rate
        self.width = width
        self.channels = channels
        self.rate_state = None
        self.pending = bytearray()
        self.encoder = opuslib.Encoder(16_000, 1, opuslib.APPLICATION_AUDIO)
        self.encoder.bitrate = 24_000

    def push(self, pcm: bytes) -> list[bytes]:
        if self.channels == 2:
            pcm = audioop.tomono(pcm, self.width, 0.5, 0.5)
        if self.width != 2:
            pcm = audioop.lin2lin(pcm, self.width, 2)
        if self.rate != 16_000:
            pcm, self.rate_state = audioop.ratecv(
                pcm, 2, 1, self.rate, 16_000, self.rate_state
            )
        self.pending.extend(pcm)
        packets = []
        while len(self.pending) >= 640:
            frame = bytes(self.pending[:640])
            del self.pending[:640]
            packets.append(self.encoder.encode(frame, 320))
        return packets


class HavpePlayback:
    """Collect typed raw-Opus downlink and report physical ESPHome playback."""

    def __init__(self, client: AudioV2Client, device) -> None:
        self.client = client
        self.device = device
        self.offer: audio_pb2.PlaybackOffer | None = None
        self.packets: list[bytes] = []
        self.task: asyncio.Task | None = None

    async def control(self, control: audio_pb2.ServerControl) -> None:
        kind = control.WhichOneof("event")
        if kind == "playback_offer":
            self.offer = control.playback_offer
            self.packets = []
        elif kind == "cancel_playback":
            cancel = control.cancel_playback
            if self.task and not self.task.done():
                self.task.cancel()
            await self.device.stop_audio()
            await self.client.acknowledge_playback(
                response_id=cancel.response_id.value,
                generation=cancel.generation,
                state=audio_pb2.PLAYBACK_STATE_CANCELLED,
            )

    async def media(self, packet: audio_pb2.PlaybackMediaPacket) -> None:
        if self.offer is None:
            raise RuntimeError("playback media arrived before offer")
        if (
            packet.response_id.value != self.offer.response_id.value
            or packet.generation != self.offer.generation
            or packet.sequence != len(self.packets)
        ):
            raise RuntimeError("stale or non-contiguous playback packet")
        self.packets.append(bytes(packet.opus_payload))
        if packet.final_packet:
            self.task = asyncio.create_task(self._play(self.offer, self.packets.copy()))

    async def _play(self, offer: audio_pb2.PlaybackOffer, packets: list[bytes]) -> None:
        decoder = opuslib.Decoder(24_000, 1)
        pcm = b"".join(decoder.decode(packet, 480) for packet in packets)
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24_000)
            writer.writeframes(pcm)
        url = serve_audio_bytes(output.getvalue(), ext="wav")
        try:
            self.device.clear_playback_states()
            await self.device.play_audio(url, announcement=True)
            await self.device.wait_playback_state("started", timeout=5.0)
            await self.client.acknowledge_playback(
                response_id=offer.response_id.value,
                generation=offer.generation,
                state=audio_pb2.PLAYBACK_STATE_STARTED,
            )
            await self.device.wait_playback_state("stopped", timeout=65.0)
            await self.client.acknowledge_playback(
                response_id=offer.response_id.value,
                generation=offer.generation,
                state=audio_pb2.PLAYBACK_STATE_DONE,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("HAVPE physical playback failed")
            await self.client.acknowledge_playback(
                response_id=offer.response_id.value,
                generation=offer.generation,
                state=audio_pb2.PLAYBACK_STATE_FAILED,
            )


async def forward_device_capture(
    reader: asyncio.StreamReader,
    client: AudioV2Client,
    *,
    capture_epoch: int,
    idle_timeout: float,
    interactive: bool,
    on_audio_chunk: Callable[[bytes, int], None] | None = None,
    on_audio_event: Callable[[str, dict], None] | None = None,
) -> None:
    encoder: PcmToOpus | None = None
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=idle_timeout)
        if not line:
            return
        header = json.loads(line)
        payload_length = int(header.get("payload_length", 0))
        payload = await reader.readexactly(payload_length) if payload_length else b""
        kind = header.get("type")
        if kind == "audio-chunk":
            if on_audio_chunk:
                on_audio_chunk(payload, len(payload))
        elif on_audio_event:
            on_audio_event(str(kind or ""), header)
        if kind == "audio-start":
            data = header.get("data") or {}
            encoder = PcmToOpus(
                rate=int(data.get("rate", 16_000)),
                width=int(data.get("width", 2)),
                channels=int(data.get("channels", 1)),
            )
            capabilities = None
            profile = audio_pb2.PROCESSING_PROFILE_SOURCE_NATIVE
            if interactive:
                profile = audio_pb2.PROCESSING_PROFILE_HALF_DUPLEX
                capabilities = audio_pb2.CaptureCapabilities(
                    duplex_mode=audio_pb2.DUPLEX_MODE_HALF,
                    input_route=audio_pb2.INPUT_ROUTE_REMOTE,
                    output_route=audio_pb2.OUTPUT_ROUTE_REMOTE,
                    native_sample_rate_hz=16_000,
                    acoustic_echo_cancellation=audio_pb2.EffectStatus(
                        requested=True, available=False, enabled=False
                    ),
                    noise_suppression=audio_pb2.EffectStatus(
                        requested=False, available=False, enabled=False
                    ),
                )
            await client.start_capture(
                capture_epoch=capture_epoch,
                processing_profile=profile,
                capabilities=capabilities,
            )
            if capabilities is not None:
                await client.voice_ready(capabilities)
        elif kind == "audio-chunk":
            if encoder is None:
                raise RuntimeError("HAVPE audio-chunk arrived before audio-start")
            for opus in encoder.push(payload):
                await client.send_opus(opus, captured_at=time.time())
        elif kind == "audio-stop":
            await client.stop_capture()
            encoder = None


async def forward_device_events(device, client: AudioV2Client) -> None:
    states = {
        "SINGLE_PRESS": audio_pb2.BUTTON_STATE_SINGLE_PRESS,
        "DOUBLE_PRESS": audio_pb2.BUTTON_STATE_DOUBLE_PRESS,
        "LONG_PRESS": audio_pb2.BUTTON_STATE_LONG_PRESS,
    }
    while True:
        event = await device.get_event()
        if event.get("type") != "button-event":
            logger.debug("HAVPE event has no audio-v2 mapping: %s", event)
            continue
        state = states.get(str(event.get("state", "")).upper())
        if state is None:
            logger.warning("Unknown HAVPE button state: %s", event.get("state"))
            continue
        await client.send_button(state)
