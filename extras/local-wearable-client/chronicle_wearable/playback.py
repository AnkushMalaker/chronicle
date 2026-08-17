"""Physical playback targets for the wearable protocol-v1 bridge."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Callable

from chronicle_client.voice_session import PlaybackReporter, VoiceTargetCapabilities
from chronicle_wearable_sdk.speaker_audio import encode_wav_to_opus_packets

from .output_route import HostOutputSelection, MacOutputRoute, MacOutputRouteDetector

StatusCallback = Callable[[str], None]


class RouteChangedPlaybackError(RuntimeError):
    error_code = "route_changed"


class ElatoPlaybackTarget:
    """Bound Opus-over-BLE output with firmware-confirmed playback state."""

    native_sample_rate = 24_000
    voice_capabilities = VoiceTargetCapabilities.half_duplex(
        native_sample_rate=native_sample_rate,
        output_route="remote",
        fallback_reason="platform_unavailable",
    )

    def __init__(
        self,
        speaker,
        *,
        packet_pacing_seconds: float = 0.057,
        on_status: StatusCallback | None = None,
    ) -> None:
        self.speaker = speaker
        self.packet_pacing_seconds = packet_pacing_seconds
        self.on_status = on_status or (lambda _status: None)
        self._status_queues: dict[tuple[str, int], asyncio.Queue[str]] = {}
        self._current: tuple[str, int] | None = None
        self._prepared = False

    async def prepare(self) -> None:
        if not self.speaker.supports_speaker_protocol_v1():
            raise RuntimeError(
                "Elato firmware lacks Chronicle speaker protocol-v1 status"
            )

        def receive_status(response_id: str, generation: int, state: str) -> None:
            queue = self._status_queues.setdefault(
                (response_id, generation), asyncio.Queue()
            )
            queue.put_nowait(state)

        await self.speaker.subscribe_speaker_status(receive_status)
        self._prepared = True

    async def _wait_for(
        self,
        response_id: str,
        generation: int,
        expected: str,
        *,
        timeout: float,
    ) -> None:
        queue = self._status_queues.setdefault(
            (response_id, generation), asyncio.Queue()
        )
        while True:
            state = await asyncio.wait_for(queue.get(), timeout=timeout)
            if state == "failed":
                raise RuntimeError("Elato firmware reported playback failure")
            if state == expected:
                return

    async def play(
        self,
        *,
        response_id: str,
        generation: int,
        wav: bytes,
        report: PlaybackReporter,
    ) -> None:
        if not self._prepared:
            raise RuntimeError("Elato playback target is not prepared")
        if self._current is not None:
            raise RuntimeError("Elato already has a current response")
        packets = encode_wav_to_opus_packets(wav)
        self._current = (response_id, generation)
        try:
            await self.speaker.speaker_start(response_id, generation)
            await self._wait_for(response_id, generation, "started", timeout=2.0)
            self.on_status("Elato speaker · TTS playing")
            await report("started", None)
            for index, packet in enumerate(packets):
                await self.speaker.write_speaker_audio(packet)
                if index >= 3 and self.packet_pacing_seconds > 0:
                    await asyncio.sleep(self.packet_pacing_seconds)
            await self.speaker.speaker_end(response_id, generation)
            await self._wait_for(response_id, generation, "done", timeout=65.0)
            await report("done", None)
            self.on_status("Elato speaker · ready")
        finally:
            self._status_queues.pop((response_id, generation), None)
            if self._current == (response_id, generation):
                self._current = None

    async def cancel(self, *, response_id: str, cancellation_generation: int) -> None:
        current = self._current
        if current is None or current[0] != response_id:
            return
        response_generation = current[1]
        if cancellation_generation < response_generation:
            return
        await self.speaker.speaker_stop(response_id, cancellation_generation)
        await self._wait_for(
            response_id,
            response_generation,
            "cancelled",
            timeout=2.0,
        )
        self.on_status("Elato speaker · TTS interrupted")


class AfplayPlaybackTarget:
    """macOS host output used for speakerless OMI devices in the tray."""

    native_sample_rate = 48_000

    def __init__(
        self,
        *,
        selection: HostOutputSelection,
        route: MacOutputRoute,
        route_detector: MacOutputRouteDetector,
        capture_allowed: asyncio.Event,
        on_status: StatusCallback | None = None,
        on_route_change=None,
    ) -> None:
        if not selection.enabled or selection.capabilities is None:
            raise ValueError("host playback target requires an enabled selection")
        self.voice_capabilities = selection.capabilities
        self.selection = selection
        self.route = route
        self.route_detector = route_detector
        self.capture_allowed = capture_allowed
        self.on_status = on_status or (lambda _status: None)
        self.on_route_change = on_route_change
        self._current: tuple[str, int, asyncio.subprocess.Process, Path] | None = None
        self._cancelled: set[str] = set()

    @property
    def _status_prefix(self) -> str:
        return f"OMI mic → {self.route.name}"

    async def _watch_route(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            observed = await self.route_detector.detect()
            if observed != self.route:
                if self.on_route_change is not None:
                    result = self.on_route_change(observed)
                    if asyncio.iscoroutine(result):
                        await result
                raise RouteChangedPlaybackError("macOS output route changed")

    async def play(
        self,
        *,
        response_id: str,
        generation: int,
        wav: bytes,
        report: PlaybackReporter,
    ) -> None:
        if self._current is not None:
            raise RuntimeError("host output already has a current response")
        descriptor, raw_path = tempfile.mkstemp(
            prefix="chronicle-response-", suffix=".wav"
        )
        path = Path(raw_path)
        gate_capture = self.voice_capabilities.mode == "duplex_half"
        if gate_capture:
            self.capture_allowed.clear()
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(wav)
            process = await asyncio.create_subprocess_exec(
                "afplay",
                str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._current = (response_id, generation, process, path)
            self.on_status(f"{self._status_prefix} · TTS playing")
            await report("started", None)
            wait_task = asyncio.create_task(process.wait())
            route_task = asyncio.create_task(self._watch_route())
            done, _ = await asyncio.wait(
                {wait_task, route_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if route_task in done:
                try:
                    await route_task
                finally:
                    if process.returncode is None:
                        process.terminate()
                    await wait_task
            route_task.cancel()
            await asyncio.gather(route_task, return_exceptions=True)
            return_code = await wait_task
            if response_id in self._cancelled:
                return
            if return_code != 0:
                raise RuntimeError(f"afplay exited with code {return_code}")
            await report("done", None)
            self.on_status(
                self.selection.status.replace(" · headphones", " · ready").replace(
                    " · speaker-safe", " · ready"
                )
            )
        finally:
            if self._current and self._current[0:2] == (response_id, generation):
                self._current = None
            self._cancelled.discard(response_id)
            if gate_capture:
                self.capture_allowed.set()
            path.unlink(missing_ok=True)

    async def cancel(self, *, response_id: str, cancellation_generation: int) -> None:
        current = self._current
        if current is None or current[0] != response_id:
            return
        if cancellation_generation < current[1]:
            return
        self._cancelled.add(response_id)
        process = current[2]
        if process.returncode is None:
            process.terminate()
        await process.wait()
        self.on_status(f"{self._status_prefix} · TTS interrupted")
