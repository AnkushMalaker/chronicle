"""Strict protocol-v1 Opus packet validation at the network ingress seam."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

MAX_RAW_OPUS_PACKET_BYTES = 1_275


class InteractiveOpusMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    codec: Literal["opus"]
    rate: Literal[16_000]
    channels: Literal[1]
    frame_duration_ms: Literal[20, 40, 60]
    time_basis: Literal["captured"]
    frame_sequence: int = Field(ge=0)
    monotonic_offset_ms: int | float = Field(ge=0)
    captured_at_ms: int | float = Field(gt=0)


class InteractiveOpusChunkHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["audio-chunk"]
    version: Literal["1.0.0"]
    data: InteractiveOpusMetadata
    payload_length: int = Field(gt=0, le=MAX_RAW_OPUS_PACKET_BYTES)


_INTERACTIVE_OPUS_HEADER = TypeAdapter(InteractiveOpusChunkHeader)


def parse_interactive_opus_chunk_header(payload: object) -> InteractiveOpusChunkHeader:
    return _INTERACTIVE_OPUS_HEADER.validate_python(payload)


def validate_raw_opus_payload(
    header: InteractiveOpusChunkHeader, payload: bytes
) -> None:
    if len(payload) != header.payload_length:
        raise ValueError(
            f"interactive Opus payload_length={header.payload_length} does not match "
            f"binary payload={len(payload)}"
        )
    if payload.startswith(b"OggS") or payload.startswith(b"OpusHead"):
        raise ValueError("interactive Opus payload must be one raw packet")
