"""Strict version-one contracts for interactive phone voice sessions.

The authenticated WebSocket supplies ``user_id``.  It is intentionally absent from
every wire model here; accepting it in an event would let a client choose the owner of
an interactive downlink.  Interactive events instead carry the complete client,
capture-session, voice-session, and capture-epoch binding.
"""

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

VOICE_DUPLEX_PROTOCOL = 1
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_DURATION_MS = 60_000
DEFAULT_READINESS_DEADLINE_MS = 2_000

ProcessingProfile = Literal[
    "ambient",
    "imported",
    "source_native",
    "duplex_aec",
    "duplex_isolated",
    "half_duplex",
]
VoiceMode = Literal["duplex_full", "duplex_isolated", "duplex_half"]
InputRoute = Literal[
    "built_in_mic",
    "bluetooth_hfp",
    "wired_mic",
    "usb",
    "unknown",
]
OutputRoute = Literal[
    "speakerphone",
    "earpiece",
    "headphones",
    "bluetooth_hfp",
    "usb",
    "remote",
    "unknown",
]


class StrictWireModel(BaseModel):
    """A wire model with no aliases or compatibility fields."""

    model_config = ConfigDict(extra="forbid")


class ProtocolEvent(StrictWireModel):
    protocol: Literal[VOICE_DUPLEX_PROTOCOL] = VOICE_DUPLEX_PROTOCOL
    event_id: UUID
    client_id: str = Field(min_length=1, max_length=255)
    sent_at: datetime

    @field_validator("sent_at")
    @classmethod
    def sent_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sent_at must include a UTC offset")
        return value


class BoundVoiceEvent(ProtocolEvent):
    audio_session_id: str = Field(min_length=1, max_length=255)
    voice_session_id: str = Field(min_length=1, max_length=255)
    capture_epoch: int = Field(ge=0)


class EffectStatus(StrictWireModel):
    requested: bool
    available: bool
    enabled: bool

    @model_validator(mode="after")
    def enabled_effect_must_be_available_and_requested(self) -> "EffectStatus":
        if self.enabled and (not self.requested or not self.available):
            raise ValueError("an enabled effect must be requested and available")
        return self


class VoiceCapabilities(StrictWireModel):
    mode: VoiceMode
    input_route: InputRoute
    output_route: OutputRoute
    native_sample_rate: int = Field(gt=0, le=384_000)
    aec: EffectStatus
    noise_suppression: EffectStatus
    fallback_reason: (
        Literal[
            "aec_unavailable",
            "aec_unhealthy",
            "route_not_isolated",
            "unsupported_route",
            "platform_unavailable",
        ]
        | None
    )

    @model_validator(mode="after")
    def full_duplex_requires_verified_speakerphone_aec(self) -> "VoiceCapabilities":
        if self.mode == "duplex_full" and (
            self.output_route != "speakerphone" or not self.aec.enabled
        ):
            raise ValueError(
                "duplex_full requires speakerphone output with enabled AEC"
            )
        if self.mode == "duplex_isolated" and self.output_route not in {
            "headphones",
            "bluetooth_hfp",
            "usb",
        }:
            raise ValueError("duplex_isolated requires an isolated output route")
        if self.mode == "duplex_half" and self.fallback_reason is None:
            raise ValueError("duplex_half requires a fallback reason")
        if self.mode != "duplex_half" and self.fallback_reason is not None:
            raise ValueError("non-fallback modes cannot report a fallback reason")
        return self


class AudioSessionStarted(ProtocolEvent):
    type: Literal["audio-session.started"]
    audio_session_id: str = Field(min_length=1, max_length=255)
    capture_epoch: int = Field(ge=0)
    processing_profile: ProcessingProfile
    voice_session_id: str | None = Field(default=None, min_length=1, max_length=255)


class VoiceSessionStart(BoundVoiceEvent):
    type: Literal["voice-session.start"]
    resume_token: str = Field(min_length=32, max_length=512)
    response_generation: int = Field(ge=0)
    readiness_deadline_ms: int = Field(
        default=DEFAULT_READINESS_DEADLINE_MS, ge=100, le=10_000
    )


class VoiceSessionReady(BoundVoiceEvent):
    type: Literal["voice-session.ready"]
    capabilities: VoiceCapabilities


class VoiceSessionCapabilitiesChanged(BoundVoiceEvent):
    type: Literal["voice-session.capabilities-changed"]
    reason: Literal[
        "route_changed",
        "interruption",
        "engine_reset",
        "effect_failed",
        "audio_focus_lost",
    ]
    capabilities: VoiceCapabilities


class VoiceSessionResume(ProtocolEvent):
    type: Literal["voice-session.resume"]
    previous_voice_session_id: str = Field(min_length=1, max_length=255)
    previous_capture_epoch: int = Field(ge=0)
    resume_token: str = Field(min_length=32, max_length=512)
    last_response_generation: int = Field(ge=0)


class VoiceSessionStop(BoundVoiceEvent):
    type: Literal["voice-session.stop"]
    reason: Literal[
        "interaction_complete",
        "user_requested",
        "audio_disconnect",
        "temporarily_unavailable",
    ]


class VoiceSessionStopped(BoundVoiceEvent):
    type: Literal["voice-session.stopped"]
    restoration_succeeded: bool
    failure_code: (
        Literal[
            "far_field_restore_failed",
            "permission_denied",
            "engine_unavailable",
        ]
        | None
    )

    @model_validator(mode="after")
    def failure_matches_restoration_result(self) -> "VoiceSessionStopped":
        if self.restoration_succeeded == (self.failure_code is not None):
            raise ValueError("failure_code is required exactly when restoration fails")
        return self


class ResponseAudio(BoundVoiceEvent):
    type: Literal["response.audio"]
    turn_id: str = Field(min_length=1, max_length=255)
    turn_revision: int = Field(ge=0)
    response_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=0)
    sequence: Literal[0]
    kind: Literal["speech", "tone"]
    barge_in_allowed: bool
    media_type: Literal["audio/wav"]
    sample_rate: int = Field(gt=0, le=384_000)
    byte_length: int = Field(gt=0, le=MAX_RESPONSE_BYTES)
    duration_ms: int = Field(gt=0, le=MAX_RESPONSE_DURATION_MS)
    payload_length: int = Field(gt=0, le=MAX_RESPONSE_BYTES)
    trace_id: str = Field(min_length=1, max_length=255)
    causation_id: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def payload_length_matches_media_length(self) -> "ResponseAudio":
        if self.payload_length != self.byte_length:
            raise ValueError("payload_length must equal byte_length")
        if self.kind == "tone" and self.barge_in_allowed:
            raise ValueError("tones cannot claim barge-in support")
        return self


class ResponseCancel(BoundVoiceEvent):
    type: Literal["response.cancel"]
    response_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=0)
    reason: Literal[
        "barge_in",
        "new_turn",
        "replacement",
        "route_change",
        "disconnect",
        "session_stopped",
    ]


class ResponsePlayback(BoundVoiceEvent):
    type: Literal["response.playback"]
    response_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=0)
    state: Literal["started", "done", "cancelled", "failed"]
    monotonic_timestamp_ms: int = Field(ge=0)
    error_code: (
        Literal[
            "decode_failed",
            "route_changed",
            "engine_reset",
            "playback_unavailable",
        ]
        | None
    )

    @model_validator(mode="after")
    def error_matches_playback_state(self) -> "ResponsePlayback":
        if self.state == "failed" and self.error_code is None:
            raise ValueError("failed playback requires error_code")
        if self.state != "failed" and self.error_code is not None:
            raise ValueError("only failed playback accepts error_code")
        return self


VoiceProtocolEvent = Annotated[
    Union[
        AudioSessionStarted,
        VoiceSessionStart,
        VoiceSessionReady,
        VoiceSessionCapabilitiesChanged,
        VoiceSessionResume,
        VoiceSessionStop,
        VoiceSessionStopped,
        ResponseAudio,
        ResponseCancel,
        ResponsePlayback,
    ],
    Field(discriminator="type"),
]

_VOICE_PROTOCOL_EVENT_ADAPTER = TypeAdapter(VoiceProtocolEvent)


def parse_voice_protocol_event(payload: object) -> VoiceProtocolEvent:
    """Validate one protocol-v1 control header with exact field rejection."""

    return _VOICE_PROTOCOL_EVENT_ADAPTER.validate_python(payload)
