"""Wake-word wire contracts at the Redis stream boundary."""

from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class WakeDetectionEvent:
    """Validated wake-word detection event from ``wakeword:detections``."""

    session_id: SessionId
    client_id: ClientId
    user_id: UserId
    audio_b64: str
    sample_rate: int
    detected_at: float
    has_speech: bool
    wakeword: str | None
    also_fired: list[str]
    score: float | None
    reason: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WakeDetectionEvent":
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
            session_id=SessionId.from_value(_required_text(payload, "session_id")),
            client_id=ClientId.from_value(_required_text(payload, "client_id")),
            user_id=UserId.from_value(_required_text(payload, "user_id")),
            audio_b64=str(payload.get("audio_b64", "")),
            sample_rate=int(payload.get("sample_rate", 16000)),
            detected_at=float(payload.get("detected_at") or 0.0),
            has_speech=bool(payload.get("has_speech", True)),
            wakeword=wakeword,
            also_fired=[str(value) for value in also_fired],
            score=_optional_float(payload, "score"),
            reason=reason,
        )

    @property
    def downlink_channel(self) -> DeviceDownlinkChannel:
        return device_downlink_channel(self.client_id)
