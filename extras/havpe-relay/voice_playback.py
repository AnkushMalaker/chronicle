"""Protocol-v1 playback target backed by the HAVPE ESPHome media player."""

from collections.abc import Callable

from chronicle_client.voice_session import PlaybackReporter, VoiceTargetCapabilities
from tone_server import serve_audio_bytes


class HavpePlaybackTarget:
    native_sample_rate = 48_000
    voice_capabilities = VoiceTargetCapabilities.half_duplex(
        native_sample_rate=native_sample_rate,
        output_route="remote",
        fallback_reason="platform_unavailable",
    )

    def __init__(
        self,
        device,
        *,
        stage_audio: Callable[[bytes], str] | None = None,
    ) -> None:
        self.device = device
        self.stage_audio = stage_audio or (
            lambda wav: serve_audio_bytes(wav, ext="wav")
        )
        self._current: tuple[str, int] | None = None
        self._cancelled: set[str] = set()

    async def play(
        self,
        *,
        response_id: str,
        generation: int,
        wav: bytes,
        report: PlaybackReporter,
    ) -> None:
        if self._current is not None:
            raise RuntimeError("HAVPE already has a current response")
        self.device.clear_playback_states()
        url = self.stage_audio(wav)
        self._current = (response_id, generation)
        try:
            await self.device.play_audio(url, announcement=True)
            await self.device.wait_playback_state("started", timeout=5.0)
            await report("started", None)
            await self.device.wait_playback_state("stopped", timeout=65.0)
            if response_id not in self._cancelled:
                await report("done", None)
        finally:
            if self._current == (response_id, generation):
                self._current = None
            self._cancelled.discard(response_id)

    async def cancel(self, *, response_id: str, cancellation_generation: int) -> None:
        current = self._current
        if current is None or current[0] != response_id:
            return
        if cancellation_generation < current[1]:
            return
        self._cancelled.add(response_id)
        await self.device.stop_audio()
        await self.device.wait_playback_state("stopped", timeout=2.0)
