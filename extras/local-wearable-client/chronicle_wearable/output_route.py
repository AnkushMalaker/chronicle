"""Conservative macOS output detection and Chronicle voice policy."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceTargetCapabilities:
    processing_profile: str
    mode: str
    native_sample_rate: int
    output_route: str
    fallback_reason: str | None = None

    @classmethod
    def isolated(cls, *, native_sample_rate: int, output_route: str):
        return cls(
            "duplex_isolated", "duplex_isolated", native_sample_rate, output_route
        )

    @classmethod
    def half_duplex(
        cls, *, native_sample_rate: int, output_route: str, fallback_reason: str
    ):
        return cls(
            "half_duplex",
            "duplex_half",
            native_sample_rate,
            output_route,
            fallback_reason,
        )


class HostOutputPolicy(str, Enum):
    AUTO = "auto"
    REQUIRE_HEADPHONES = "require_headphones"
    ALWAYS_HALF_DUPLEX = "always_half_duplex"


@dataclass(frozen=True)
class MacOutputRoute:
    name: str
    output_route: str
    isolated: bool


@dataclass(frozen=True)
class HostOutputSelection:
    enabled: bool
    processing_profile: str | None
    capabilities: VoiceTargetCapabilities | None
    status: str


_ISOLATED_NAME_PARTS = (
    "airpod",
    "earbud",
    "earphone",
    "headphone",
    "headset",
    "beats fit",
    "beats studio",
    "beats solo",
)


def _find_default_output(value) -> dict | None:
    if isinstance(value, dict):
        if value.get("coreaudio_default_audio_output_device") == "spaudio_yes":
            return value
        for child in value.values():
            found = _find_default_output(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_default_output(child)
            if found is not None:
                return found
    return None


def parse_system_profiler_audio(payload: bytes) -> MacOutputRoute:
    """Read the default output from ``system_profiler`` JSON.

    A route is called isolated only when its displayed name explicitly identifies
    headphones or earbuds. Generic USB and Bluetooth devices may be speakers, so
    they deliberately remain speaker-safe.
    """

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("macOS audio inventory was not valid JSON") from error
    default = _find_default_output(document)
    if default is None:
        raise ValueError("macOS did not report a default output device")
    name = str(default.get("_name", "")).strip()
    if not name:
        raise ValueError("macOS default output has no display name")
    lowered = name.casefold()
    isolated = any(part in lowered for part in _ISOLATED_NAME_PARTS)
    if isolated:
        output_route = "headphones"
    elif "speaker" in lowered or "display" in lowered:
        output_route = "speakerphone"
    elif "usb" in lowered:
        output_route = "usb"
    else:
        output_route = "unknown"
    return MacOutputRoute(name=name, output_route=output_route, isolated=isolated)


def resolve_host_output(
    policy: HostOutputPolicy, route: MacOutputRoute
) -> HostOutputSelection:
    prefix = f"OMI mic → {route.name}"
    if policy == HostOutputPolicy.REQUIRE_HEADPHONES and not route.isolated:
        return HostOutputSelection(
            enabled=False,
            processing_profile=None,
            capabilities=None,
            status=f"{prefix} · headphones required",
        )
    if route.isolated and policy != HostOutputPolicy.ALWAYS_HALF_DUPLEX:
        capabilities = VoiceTargetCapabilities.isolated(
            native_sample_rate=48_000,
            output_route=route.output_route,
        )
        return HostOutputSelection(
            enabled=True,
            processing_profile=capabilities.processing_profile,
            capabilities=capabilities,
            status=f"{prefix} · headphones",
        )
    capabilities = VoiceTargetCapabilities.half_duplex(
        native_sample_rate=48_000,
        output_route=route.output_route,
        fallback_reason=(
            "platform_unavailable"
            if policy == HostOutputPolicy.ALWAYS_HALF_DUPLEX
            else "route_not_isolated"
        ),
    )
    return HostOutputSelection(
        enabled=True,
        processing_profile=capabilities.processing_profile,
        capabilities=capabilities,
        status=f"{prefix} · speaker-safe",
    )


class MacOutputRouteDetector:
    """Query the actual macOS default output; failures safely become unknown."""

    async def detect(self) -> MacOutputRoute:
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/sbin/system_profiler",
                "SPAudioDataType",
                "-json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8.0)
            if process.returncode != 0:
                raise RuntimeError(f"system_profiler exited {process.returncode}")
            return parse_system_profiler_audio(stdout)
        except Exception as error:
            logger.warning("Could not verify macOS output route: %s", error)
            return MacOutputRoute(
                name="Unknown output",
                output_route="unknown",
                isolated=False,
            )
