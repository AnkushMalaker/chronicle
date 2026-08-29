import pytest
from chronicle_wearable.output_route import (
    HostOutputPolicy,
    parse_system_profiler_audio,
    resolve_host_output,
)


def _profile(name: str) -> bytes:
    return (
        '{"SPAudioDataType":[{"_name":"Devices","_items":['
        f'{{"_name":"{name}",'
        '"coreaudio_default_audio_output_device":"spaudio_yes"}]}]}'
    ).encode()


def test_airpods_are_verified_as_isolated_output():
    route = parse_system_profiler_audio(_profile("Alex’s AirPods Pro"))
    selection = resolve_host_output(HostOutputPolicy.AUTO, route)

    assert selection.enabled
    assert selection.processing_profile == "duplex_isolated"
    assert selection.capabilities.mode == "duplex_isolated"
    assert selection.capabilities.output_route == "headphones"
    assert selection.status == "OMI mic → Alex’s AirPods Pro · headphones"


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
