"""Wake-word wire contracts at the Redis stream boundary."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from advanced_omi_backend.redis_keys import (
    ClientId,
    DeviceDownlinkChannel,
    SessionId,
    UserId,
    device_downlink_channel,
)


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if value is None:
        raise ValueError(f"wake detection event missing {field}")
    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, str):
        raise TypeError(f"wake detection event field {field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"wake detection event field {field} cannot be empty")
    return value


def _optional_float(payload: dict[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    return float(value)


def _required_float(payload: dict[str, Any], field: str) -> float:
    value = payload.get(field)
    if value is None:
        raise ValueError(f"wake detection event missing {field}")
    value = float(value)
    if value <= 0:
        raise ValueError(f"wake detection event field {field} must be positive")
    return value


@dataclass(frozen=True)
class WakeDetectionInterval:
    start_ms: float
    end_ms: float
    started_at: float
    ended_at: float

    @classmethod
    def from_payload(cls, payload: Any, field: str) -> "WakeDetectionInterval":
        if not isinstance(payload, dict):
            raise TypeError(f"wake detection event field {field} must be an object")
        interval = cls(
            start_ms=float(payload.get("start_ms", -1)),
            end_ms=float(payload.get("end_ms", -1)),
            started_at=_required_float(payload, "started_at"),
            ended_at=_required_float(payload, "ended_at"),
        )
        if interval.start_ms < 0 or interval.end_ms <= interval.start_ms:
            raise ValueError(
                f"wake detection event field {field} has invalid native bounds"
            )
        if interval.ended_at <= interval.started_at:
            raise ValueError(
                f"wake detection event field {field} has invalid absolute bounds"
            )
        return interval


@dataclass(frozen=True)
class WakeDetectionEvent:
    """Validated wake-word detection event from ``wakeword:detections``."""

    session_id: SessionId
    client_id: ClientId
    user_id: UserId
    audio_b64: str
    sample_rate: int
    wake_trace_id: str
    capture_epoch: int
    armed_at: float
    end_of_turn_at: float
    trigger_interval: WakeDetectionInterval
    command_interval: WakeDetectionInterval
    has_speech: bool
    wakeword: str | None
    also_fired: list[str]
    score: float | None
    reason: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WakeDetectionEvent":
        session_id = SessionId.from_value(_required_text(payload, "session_id"))
        client_id = ClientId.from_value(_required_text(payload, "client_id"))
        user_id = UserId.from_value(_required_text(payload, "user_id"))
        wake_trace_id = _required_text(payload, "wake_trace_id")
        try:
            UUID(wake_trace_id)
        except ValueError as error:
            raise ValueError(
                "wake detection event field wake_trace_id must be a UUID"
            ) from error
        capture_epoch = int(payload.get("capture_epoch", -1))
        if capture_epoch < 0:
            raise ValueError(
                "wake detection event field capture_epoch must be non-negative"
            )
        armed_at = _required_float(payload, "armed_at")
        end_of_turn_at = _required_float(payload, "end_of_turn_at")
        if end_of_turn_at < armed_at:
            raise ValueError(
                "wake detection event end_of_turn_at cannot precede armed_at"
            )

        also_fired = payload.get("also_fired", [])
        if also_fired is None:
            also_fired = []
        if not isinstance(also_fired, list):
            raise TypeError("wake detection event field also_fired must be a list")

        wakeword = payload.get("wakeword")
        if wakeword is not None:
            wakeword = str(wakeword)

        reason = payload.get("reason")
        if reason is not None:
            reason = str(reason)

        return cls(
            session_id=session_id,
            client_id=client_id,
            user_id=user_id,
            audio_b64=str(payload.get("audio_b64", "")),
            sample_rate=int(payload.get("sample_rate", 16000)),
            wake_trace_id=wake_trace_id,
            capture_epoch=capture_epoch,
            armed_at=armed_at,
            end_of_turn_at=end_of_turn_at,
            trigger_interval=WakeDetectionInterval.from_payload(
                payload.get("trigger_interval"), "trigger_interval"
            ),
            command_interval=WakeDetectionInterval.from_payload(
                payload.get("command_interval"), "command_interval"
            ),
            has_speech=bool(payload.get("has_speech", True)),
            wakeword=wakeword,
            also_fired=[str(value) for value in also_fired],
            score=_optional_float(payload, "score"),
            reason=reason,
        )

    @property
    def downlink_channel(self) -> DeviceDownlinkChannel:
        return device_downlink_channel(self.client_id)
