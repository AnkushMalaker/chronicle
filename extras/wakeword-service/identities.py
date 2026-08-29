"""Runtime-distinct identities and Redis names for the wakeword service."""

from dataclasses import dataclass


@dataclass(frozen=True)
class _Identity:
    value: str

    @classmethod
    def from_value(cls, value: object, field_name: str | None = None):
        label = field_name or cls.__name__
        if value is None:
            raise ValueError(f"{label} is required")
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"{label} cannot be empty")
        return cls(value)

    def __str__(self) -> str:
        return self.value


class ClientId(_Identity):
    """Stable connected device identity."""


class SessionId(_Identity):
    """Per-WebSocket/audio-stream identity."""


class UserId(_Identity):
    """Chronicle user identity."""


class AudioStreamName(_Identity):
    """Redis stream name for raw audio WAL entries."""


class AudioSessionKey(_Identity):
    """Redis hash key for session metadata."""


class DeviceDownlinkChannel(_Identity):
    """Redis Pub/Sub channel consumed by the device WebSocket."""


@dataclass(frozen=True)
class AudioSessionRef:
    session_id: SessionId
    client_id: ClientId
    capture_epoch: int
    started_at: float

    def __post_init__(self) -> None:
        if self.capture_epoch < 0:
            raise ValueError("capture_epoch must be non-negative")
        if self.started_at <= 0:
            raise ValueError("audio session started_at must be positive")


@dataclass(frozen=True)
class AudioChunkRef:
    """Authoritative time metadata attached to one raw-audio WAL chunk."""

    captured_at: float
    time_basis: str
    sample_rate: int
    channels: int
    sample_width: int

    def __post_init__(self) -> None:
        if self.captured_at <= 0:
            raise ValueError("captured_at must be positive")
        if self.time_basis not in {"captured", "recorded", "received"}:
            raise ValueError("unknown audio chunk time_basis")
        if min(self.sample_rate, self.channels, self.sample_width) <= 0:
            raise ValueError("audio chunk format values must be positive")


def _identity_value(identity: _Identity | str) -> str:
    return identity.value if isinstance(identity, _Identity) else identity


def _require_identity(value: object, expected_type: type[_Identity]) -> _Identity:
    if not isinstance(value, expected_type):
        raise TypeError(
            f"expected {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


def audio_session_key(session_id: SessionId) -> AudioSessionKey:
    session_id = _require_identity(session_id, SessionId)
    return AudioSessionKey.from_value(f"audio:session:{session_id}")


def parse_audio_stream_name(stream_name: str | AudioStreamName) -> SessionId:
    value = _identity_value(stream_name)
    prefix = "audio:v2:realtime:"
    if not value.startswith(prefix):
        raise ValueError(f"not an audio stream name: {value!r}")
    return SessionId.from_value(value.removeprefix(prefix), "session_id")


def device_downlink_channel(client_id: ClientId) -> DeviceDownlinkChannel:
    client_id = _require_identity(client_id, ClientId)
    return DeviceDownlinkChannel.from_value(f"device:downlink:{client_id}")
