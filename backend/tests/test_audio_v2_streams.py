from datetime import datetime, timezone

import pytest
from fakeredis import aioredis as fake_aioredis
from google.protobuf import timestamp_pb2

from backend.audio_contract.v2 import audio_pb2
from backend.services.audio_stream.v2_streams import (
    DURABLE_GROUP,
    REALTIME_GROUPS,
    AudioV2Streams,
    parse_stream_event,
)

pytestmark = pytest.mark.unit


def _binding() -> audio_pb2.CaptureBinding:
    return audio_pb2.CaptureBinding(
        capture_session_id=audio_pb2.CaptureSessionId(value="capture-1"),
        capture_epoch=4,
    )


def _opened() -> audio_pb2.CaptureStreamEvent:
    return audio_pb2.CaptureStreamEvent(
        opened=audio_pb2.CaptureStreamOpened(binding=_binding())
    )


def _frame(delivery_class: int) -> audio_pb2.CaptureStreamEvent:
    captured_at = timestamp_pb2.Timestamp()
    captured_at.FromDatetime(datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc))
    return audio_pb2.CaptureStreamEvent(
        frame=audio_pb2.CanonicalPcmFrame(
            binding=_binding(),
            sequence=1,
            captured_at=captured_at,
            delivery_class=delivery_class,
            pcm_s16le=b"\x00\x00" * 320,
        )
    )


async def _events(redis_client, stream: str):
    entries = await redis_client.xrange(stream)
    return [parse_stream_event(fields) for _entry_id, fields in entries]


async def test_live_capture_opens_all_groups_before_frames_and_fans_out():
    redis_client = fake_aioredis.FakeRedis()
    streams = await AudioV2Streams.open(
        redis_client,
        event=_opened(),
        delivery_class=audio_pb2.DELIVERY_CLASS_LIVE,
    )

    durable_groups = await redis_client.xinfo_groups(streams.durable)
    realtime_groups = await redis_client.xinfo_groups(streams.realtime)
    assert {group["name"].decode() for group in durable_groups} == {DURABLE_GROUP}
    assert {group["name"].decode() for group in realtime_groups} == set(REALTIME_GROUPS)

    await streams.publish_frame(_frame(audio_pb2.DELIVERY_CLASS_LIVE))
    assert [
        event.WhichOneof("event")
        for event in await _events(redis_client, streams.durable)
    ] == ["opened", "frame"]
    assert [
        event.WhichOneof("event")
        for event in await _events(redis_client, streams.realtime)
    ] == ["opened", "frame"]


async def test_recovered_capture_has_no_realtime_stream_or_groups():
    redis_client = fake_aioredis.FakeRedis()
    streams = await AudioV2Streams.open(
        redis_client,
        event=_opened(),
        delivery_class=audio_pb2.DELIVERY_CLASS_RECOVERED,
    )

    await streams.publish_frame(_frame(audio_pb2.DELIVERY_CLASS_RECOVERED))

    assert await redis_client.exists(streams.durable)
    assert not await redis_client.exists(streams.realtime)
    assert [
        event.WhichOneof("event")
        for event in await _events(redis_client, streams.durable)
    ] == ["opened", "frame"]


async def test_stream_entries_reject_freeform_fields():
    with pytest.raises(ValueError, match="only the event field"):
        parse_stream_event({b"event": _opened().SerializeToString(), b"codec": b"pcm"})
