"""Typed Chronicle audio-v2 Redis stream boundaries.

Durability and realtime eligibility are different claims and therefore use different
streams. Every entry is one serialized ``CaptureStreamEvent``; field dictionaries are
not part of this interface.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.exceptions import ResponseError

from backend.audio_contract.v2 import audio_pb2

EVENT_FIELD = b"event"
DURABLE_GROUP = "audio-persistence-v2"
REALTIME_GROUPS = (
    "streaming-transcription-v2",
    "wakeword-v2",
    "interactive-turn-v2",
)


def durable_stream(capture_session_id: str) -> str:
    return f"audio:v2:durable:{capture_session_id}"


def realtime_stream(capture_session_id: str) -> str:
    return f"audio:v2:realtime:{capture_session_id}"


def parse_realtime_stream_name(stream_name: str) -> str:
    prefix = "audio:v2:realtime:"
    if not stream_name.startswith(prefix) or not stream_name.removeprefix(prefix):
        raise ValueError(f"invalid audio-v2 realtime stream name {stream_name!r}")
    return stream_name.removeprefix(prefix)


async def _create_group(redis_client, stream: str, group: str) -> None:
    try:
        await redis_client.xgroup_create(stream, group, id="0")
    except ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


@dataclass(frozen=True)
class AudioV2Streams:
    """Open stream names and the delivery eligibility fixed for one capture."""

    redis_client: object
    capture_session_id: str
    delivery_class: int

    @property
    def durable(self) -> str:
        return durable_stream(self.capture_session_id)

    @property
    def realtime(self) -> str:
        return realtime_stream(self.capture_session_id)

    @classmethod
    async def open(
        cls,
        redis_client,
        *,
        event: audio_pb2.CaptureStreamEvent,
        delivery_class: int,
    ) -> "AudioV2Streams":
        if event.WhichOneof("event") != "opened":
            raise ValueError("the first stream event must be opened")
        capture_session_id = event.opened.binding.capture_session_id.value
        if not capture_session_id:
            raise ValueError("opened event requires capture_session_id")
        if delivery_class not in {
            audio_pb2.DELIVERY_CLASS_LIVE,
            audio_pb2.DELIVERY_CLASS_RECOVERED,
        }:
            raise ValueError("capture requires an explicit delivery class")

        streams = cls(redis_client, capture_session_id, delivery_class)
        payload = {EVENT_FIELD: event.SerializeToString()}
        await redis_client.xadd(streams.durable, payload)
        await _create_group(redis_client, streams.durable, DURABLE_GROUP)
        if delivery_class == audio_pb2.DELIVERY_CLASS_LIVE:
            await redis_client.xadd(streams.realtime, payload)
            for group in REALTIME_GROUPS:
                await _create_group(redis_client, streams.realtime, group)
        return streams

    async def publish_frame(self, event: audio_pb2.CaptureStreamEvent) -> None:
        if event.WhichOneof("event") != "frame":
            raise ValueError("publish_frame requires a frame event")
        frame = event.frame
        if frame.binding.capture_session_id.value != self.capture_session_id:
            raise ValueError("frame has a stale capture binding")
        if frame.delivery_class != self.delivery_class:
            raise ValueError("frame delivery class differs from opened stream")
        payload = {EVENT_FIELD: event.SerializeToString()}
        await self.redis_client.xadd(self.durable, payload)
        if self.delivery_class == audio_pb2.DELIVERY_CLASS_LIVE:
            await self.redis_client.xadd(self.realtime, payload)

    async def end(self, event: audio_pb2.CaptureStreamEvent) -> None:
        if event.WhichOneof("event") not in {"ended", "failed"}:
            raise ValueError("end requires a terminal event")
        payload = {EVENT_FIELD: event.SerializeToString()}
        await self.redis_client.xadd(self.durable, payload)
        if self.delivery_class == audio_pb2.DELIVERY_CLASS_LIVE:
            await self.redis_client.xadd(self.realtime, payload)


def parse_stream_event(fields: dict[bytes, bytes]) -> audio_pb2.CaptureStreamEvent:
    if set(fields) != {EVENT_FIELD}:
        raise ValueError("audio-v2 stream entry must contain only the event field")
    event = audio_pb2.CaptureStreamEvent()
    event.ParseFromString(fields[EVENT_FIELD])
    if event.WhichOneof("event") is None:
        raise ValueError("audio-v2 stream event is empty")
    return event
