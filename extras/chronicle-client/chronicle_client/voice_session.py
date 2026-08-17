"""Protocol-v1 response session shared by Chronicle client bridges."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

VOICE_DUPLEX_PROTOCOL = 1
logger = logging.getLogger(__name__)

PlaybackReporter = Callable[[str, str | None], Awaitable[None]]


@dataclass(frozen=True)
class VoiceTargetCapabilities:
    """Verified capture profile and wire capabilities for one output target."""

    processing_profile: str
    mode: str
    input_route: str
    output_route: str
    native_sample_rate: int
    fallback_reason: str | None

    @classmethod
    def half_duplex(
        cls,
        *,
        native_sample_rate: int,
        output_route: str,
        fallback_reason: str,
    ) -> "VoiceTargetCapabilities":
        return cls(
            processing_profile="half_duplex",
            mode="duplex_half",
            input_route="unknown",
            output_route=output_route,
            native_sample_rate=native_sample_rate,
            fallback_reason=fallback_reason,
        )

    @classmethod
    def isolated(
        cls,
        *,
        native_sample_rate: int,
        output_route: str,
    ) -> "VoiceTargetCapabilities":
        if output_route not in {"headphones", "bluetooth_hfp", "usb"}:
            raise ValueError("isolated output must be headphones, Bluetooth, or USB")
        return cls(
            processing_profile="duplex_isolated",
            mode="duplex_isolated",
            input_route="unknown",
            output_route=output_route,
            native_sample_rate=native_sample_rate,
            fallback_reason=None,
        )

    def as_wire(self) -> dict:
        effect = {"requested": False, "available": False, "enabled": False}
        return {
            "mode": self.mode,
            "input_route": self.input_route,
            "output_route": self.output_route,
            "native_sample_rate": self.native_sample_rate,
            "aec": effect.copy(),
            "noise_suppression": effect.copy(),
            "fallback_reason": self.fallback_reason,
        }


class PlaybackTarget(Protocol):
    """One real output route controlled by the tray/relay client."""

    native_sample_rate: int
    voice_capabilities: VoiceTargetCapabilities

    async def play(
        self,
        *,
        response_id: str,
        generation: int,
        wav: bytes,
        report: PlaybackReporter,
    ) -> None: ...

    async def cancel(
        self, *, response_id: str, cancellation_generation: int
    ) -> None: ...


class WearableVoiceProtocolError(RuntimeError):
    """The peer sent an invalid or stale protocol-v1 event."""


class ServerUpgradeRequired(WearableVoiceProtocolError):
    """The backend did not complete the advertised protocol-v1 handshake."""


@dataclass(frozen=True)
class VoiceBinding:
    client_id: str
    audio_session_id: str
    voice_session_id: str
    capture_epoch: int


class WearableVoiceSession:
    """Validate and run Chronicle v1 against one verified playback target."""

    def __init__(
        self,
        websocket,
        *,
        capture_epoch: int,
        playback_target: PlaybackTarget | None,
        send_lock: asyncio.Lock | None = None,
    ) -> None:
        if capture_epoch < 0:
            raise ValueError("capture_epoch must be non-negative")
        self.websocket = websocket
        self.capture_epoch = capture_epoch
        self.playback_target = playback_target
        self.send_lock = send_lock
        self.audio_binding: tuple[str, str, str] | None = None
        self.voice_binding: VoiceBinding | None = None
        self.pending_audio: dict | None = None
        self.last_generation = 0
        self._playback_task: asyncio.Task | None = None
        self._playing_header: dict | None = None
        self._ready = asyncio.Event()

    @property
    def interactive(self) -> bool:
        return self.playback_target is not None

    def audio_start_data(self) -> dict:
        data = {
            "rate": 16000,
            "width": 2,
            "channels": 1,
            "mode": "streaming",
        }
        if not self.interactive:
            return data
        target = self.playback_target
        if target is None:
            return data
        data.update(
            {
                "voice_duplex_protocol": VOICE_DUPLEX_PROTOCOL,
                "capture_epoch": self.capture_epoch,
                "processing_profile": target.voice_capabilities.processing_profile,
                "effects": {
                    "aec": {
                        "requested": False,
                        "available": False,
                        "enabled": False,
                    },
                    "noise_suppression": {
                        "requested": False,
                        "available": False,
                        "enabled": False,
                    },
                },
                "voice_session_id": None,
            }
        )
        return data

    async def _send(self, payload: dict) -> None:
        message = json.dumps(payload, separators=(",", ":")) + "\n"
        if self.send_lock is None:
            await self.websocket.send(message)
            return
        async with self.send_lock:
            await self.websocket.send(message)

    def _assert_protocol(self, event: dict) -> None:
        if event.get("protocol") != VOICE_DUPLEX_PROTOCOL:
            raise WearableVoiceProtocolError("unsupported voice protocol")
        if event.get("capture_epoch") != self.capture_epoch:
            raise WearableVoiceProtocolError("stale capture epoch")

    def _assert_voice_binding(self, event: dict) -> VoiceBinding:
        self._assert_protocol(event)
        binding = self.voice_binding
        if binding is None:
            raise WearableVoiceProtocolError("voice session has not started")
        observed = VoiceBinding(
            client_id=str(event.get("client_id", "")),
            audio_session_id=str(event.get("audio_session_id", "")),
            voice_session_id=str(event.get("voice_session_id", "")),
            capture_epoch=int(event.get("capture_epoch", -1)),
        )
        if observed != binding:
            raise WearableVoiceProtocolError("stale voice-session binding")
        return binding

    async def handle_event(self, event: dict) -> bool:
        """Handle one server control event; return whether v1 consumed it."""

        event_type = event.get("type")
        if event_type == "audio-session.started":
            if not self.interactive:
                raise WearableVoiceProtocolError(
                    "server started interactive voice without a playback target"
                )
            self._assert_protocol(event)
            target = self.playback_target
            if target is None:
                raise WearableVoiceProtocolError("playback target disappeared")
            if (
                event.get("processing_profile")
                != target.voice_capabilities.processing_profile
            ):
                raise WearableVoiceProtocolError(
                    "audio session does not match verified target profile"
                )
            client_id = str(event.get("client_id", ""))
            audio_session_id = str(event.get("audio_session_id", ""))
            voice_session_id = str(event.get("voice_session_id", ""))
            if not client_id or not audio_session_id or not voice_session_id:
                raise WearableVoiceProtocolError("audio session binding is incomplete")
            self.audio_binding = (client_id, audio_session_id, voice_session_id)
            return True

        if event_type == "voice-session.start":
            self._assert_protocol(event)
            if self.audio_binding != (
                event.get("client_id"),
                event.get("audio_session_id"),
                event.get("voice_session_id"),
            ):
                raise WearableVoiceProtocolError("voice start is not audio-bound")
            voice_session_id = str(event.get("voice_session_id", ""))
            if not voice_session_id:
                raise WearableVoiceProtocolError("voice session id is missing")
            self.voice_binding = VoiceBinding(
                client_id=str(event["client_id"]),
                audio_session_id=str(event["audio_session_id"]),
                voice_session_id=voice_session_id,
                capture_epoch=self.capture_epoch,
            )
            self.last_generation = int(event.get("response_generation", 0))
            target = self.playback_target
            if target is None:
                raise WearableVoiceProtocolError("playback target disappeared")
            await self._send_bound(
                "voice-session.ready",
                capabilities=target.voice_capabilities.as_wire(),
            )
            self._ready.set()
            return True

        if event_type == "response.audio":
            self._assert_voice_binding(event)
            generation = int(event.get("generation", -1))
            if generation < self.last_generation:
                raise WearableVoiceProtocolError("stale response generation")
            if self.pending_audio is not None:
                raise WearableVoiceProtocolError("response payload is already pending")
            if event.get("media_type") != "audio/wav":
                raise WearableVoiceProtocolError("unsupported response media type")
            if int(event.get("payload_length", -1)) != int(
                event.get("byte_length", -2)
            ):
                raise WearableVoiceProtocolError(
                    "response payload length is inconsistent"
                )
            self.last_generation = generation
            self.pending_audio = event
            return True

        if event_type == "response.cancel":
            self._assert_voice_binding(event)
            cancellation_generation = int(event.get("generation", -1))
            if cancellation_generation < self.last_generation:
                raise WearableVoiceProtocolError("stale cancellation generation")
            self.last_generation = cancellation_generation
            if self.pending_audio and self.pending_audio.get(
                "response_id"
            ) == event.get("response_id"):
                self.pending_audio = None
            if self._playing_header and self._playing_header.get(
                "response_id"
            ) == event.get("response_id"):
                await self._cancel_playback(event)
            return True

        if event_type == "voice-session.stop":
            self._assert_voice_binding(event)
            if self._playing_header is not None:
                await self._cancel_playback(event)
            await self._send_bound(
                "voice-session.stopped",
                restoration_succeeded=True,
                failure_code=None,
            )
            self.voice_binding = None
            self.audio_binding = None
            self.pending_audio = None
            return True

        return False

    async def handle_binary(self, payload: bytes) -> None:
        header = self.pending_audio
        self.pending_audio = None
        if header is None:
            raise WearableVoiceProtocolError(
                "binary response arrived without response.audio"
            )
        self._assert_voice_binding(header)
        if len(payload) != int(header["byte_length"]):
            raise WearableVoiceProtocolError(
                "binary response length does not match response.audio"
            )
        if self._playback_task is not None and not self._playback_task.done():
            raise WearableVoiceProtocolError("one response is already playing")
        self._playing_header = header
        self._playback_task = asyncio.create_task(
            self._run_playback(header, payload),
            name=f"wearable-response-{header['response_id']}",
        )

    async def _run_playback(self, header: dict, wav: bytes) -> None:
        target = self.playback_target
        if target is None:
            return

        async def report(state: str, error_code: str | None) -> None:
            await self._send_playback(header, state, error_code)

        try:
            await target.play(
                response_id=header["response_id"],
                generation=int(header["generation"]),
                wav=wav,
                report=report,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._send_playback(
                header,
                "failed",
                getattr(error, "error_code", "playback_unavailable"),
            )
        finally:
            if self._playing_header is header:
                self._playing_header = None

    async def _cancel_playback(self, event: dict) -> None:
        header = self._playing_header
        target = self.playback_target
        if header is None or target is None:
            return
        await target.cancel(
            response_id=str(event["response_id"]),
            cancellation_generation=int(event["generation"]),
        )
        task = self._playback_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._playing_header = None
        await self._send_playback(header, "cancelled", None)

    async def _send_bound(self, event_type: str, **data) -> None:
        binding = self.voice_binding
        if binding is None:
            raise WearableVoiceProtocolError("cannot send without voice binding")
        await self._send(
            {
                "type": event_type,
                "protocol": VOICE_DUPLEX_PROTOCOL,
                "event_id": str(uuid.uuid4()),
                "client_id": binding.client_id,
                "audio_session_id": binding.audio_session_id,
                "voice_session_id": binding.voice_session_id,
                "capture_epoch": binding.capture_epoch,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                **data,
            }
        )

    async def _send_playback(
        self, header: dict, state: str, error_code: str | None
    ) -> None:
        await self._send_bound(
            "response.playback",
            response_id=header["response_id"],
            generation=int(header["generation"]),
            state=state,
            monotonic_timestamp_ms=round(time.monotonic() * 1000),
            error_code=error_code,
        )

    async def wait_for_playback(self) -> None:
        task = self._playback_task
        if task is not None:
            await task

    async def wait_until_ready(self, timeout: float = 5.0) -> None:
        """Fail closed when a backend does not understand protocol v1."""
        if not self.interactive:
            return
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise ServerUpgradeRequired(
                "backend did not complete voice protocol-v1 startup"
            ) from error

    async def close(self) -> None:
        header = self._playing_header
        target = self.playback_target
        if header is not None and target is not None:
            try:
                await target.cancel(
                    response_id=str(header["response_id"]),
                    cancellation_generation=max(
                        self.last_generation, int(header["generation"])
                    ),
                )
            except Exception:
                logger.exception("Failed to cancel physical playback during close")
        task = self._playback_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
