import asyncio

import pytest
from chronicle_wearable.output_route import (
    HostOutputPolicy,
    parse_system_profiler_audio,
    resolve_host_output,
)
from chronicle_wearable.playback import AfplayPlaybackTarget


def _profile(name: str) -> bytes:
    return (
        '{"SPAudioDataType":[{"_name":"Devices","_items":['
        f'{{"_name":"{name}",'
        '"coreaudio_default_audio_output_device":"spaudio_yes"}]}]}'
    ).encode()


def test_airpods_are_verified_as_isolated_output():
    route = parse_system_profiler_audio(_profile("Ankush’s AirPods Pro"))
    selection = resolve_host_output(HostOutputPolicy.AUTO, route)

    assert selection.enabled
    assert selection.processing_profile == "duplex_isolated"
    assert selection.capabilities.mode == "duplex_isolated"
    assert selection.capabilities.output_route == "headphones"
    assert selection.status == "OMI mic → Ankush’s AirPods Pro · headphones"


def test_builtin_speakers_fall_back_to_capture_gated_half_duplex():
    route = parse_system_profiler_audio(_profile("MacBook Pro Speakers"))
    selection = resolve_host_output(HostOutputPolicy.AUTO, route)

    assert selection.enabled
    assert selection.processing_profile == "half_duplex"
    assert selection.capabilities.mode == "duplex_half"
    assert selection.capabilities.output_route == "speakerphone"
    assert selection.status == "OMI mic → MacBook Pro Speakers · speaker-safe"


def test_headphones_required_fails_closed_on_speakers():
    route = parse_system_profiler_audio(_profile("MacBook Pro Speakers"))
    selection = resolve_host_output(HostOutputPolicy.REQUIRE_HEADPHONES, route)

    assert not selection.enabled
    assert selection.status == "OMI mic → MacBook Pro Speakers · headphones required"


def test_missing_or_unknown_route_is_never_assumed_isolated():
    with pytest.raises(ValueError):
        parse_system_profiler_audio(b'{"SPAudioDataType": []}')

    route = parse_system_profiler_audio(_profile("Studio Monitor Output"))
    assert resolve_host_output(HostOutputPolicy.AUTO, route).processing_profile == (
        "half_duplex"
    )


@pytest.mark.asyncio
async def test_speaker_playback_gates_capture_and_surfaces_interruption(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.finished = asyncio.Event()

        async def wait(self):
            await self.finished.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.finished.set()

    class StableDetector:
        async def detect(self):
            return parse_system_profiler_audio(_profile("MacBook Pro Speakers"))

    process = FakeProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    route = await StableDetector().detect()
    selection = resolve_host_output(HostOutputPolicy.AUTO, route)
    capture_allowed = asyncio.Event()
    capture_allowed.set()
    statuses = []
    target = AfplayPlaybackTarget(
        selection=selection,
        route=route,
        route_detector=StableDetector(),
        capture_allowed=capture_allowed,
        on_status=statuses.append,
    )
    task = asyncio.create_task(
        target.play(
            response_id="response-1",
            generation=8,
            wav=b"RIFF",
            report=lambda _state, _error: asyncio.sleep(0),
        )
    )
    await asyncio.sleep(0)

    assert not capture_allowed.is_set()
    assert statuses[-1] == "OMI mic → MacBook Pro Speakers · TTS playing"

    await target.cancel(response_id="response-1", cancellation_generation=9)
    await task

    assert capture_allowed.is_set()
    assert statuses[-1] == "OMI mic → MacBook Pro Speakers · TTS interrupted"
