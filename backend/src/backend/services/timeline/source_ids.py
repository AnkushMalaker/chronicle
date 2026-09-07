"""Typed ScreenPipe source identifiers shared by ingest and Timeline evidence."""

from dataclasses import dataclass

from backend.models.timeline import EvidenceLocator


@dataclass(frozen=True)
class ScreenPipeSegmentSourceId:
    """Parsed identity for one ScreenPipe conversation segment.

    Legacy identifiers did not carry a provider-local audio track. The oldest
    also omitted direction. Their final component is a source-item range and
    must not be mistaken for either field.
    """

    capture_source_id: str
    direction: str
    track_id: str | None
    source_range: str


def _source_id_component(value: object, name: str, *, allow_colon: bool = False) -> str:
    component = str(value).strip()
    if not component:
        raise ValueError(f"ScreenPipe {name} must not be empty")
    if not allow_colon and ":" in component:
        raise ValueError(f"ScreenPipe {name} must not contain ':'")
    return component


def format_screenpipe_segment_source_id(
    capture_source_id: str,
    direction: str,
    track_id: str,
    first_source_item_id: str,
    last_source_item_id: str,
) -> str:
    """Format the current track-aware ScreenPipe segment identity."""

    source = _source_id_component(capture_source_id, "capture source id")
    lane_direction = _source_id_component(direction, "direction")
    track = _source_id_component(track_id, "track id", allow_colon=True)
    first = _source_id_component(first_source_item_id, "first source item id")
    last = _source_id_component(last_source_item_id, "last source item id")
    return f"screenpipe:{source}:{lane_direction}:{track}:{first}-{last}"


def parse_screenpipe_segment_source_id(
    external_source_id: str | None,
) -> ScreenPipeSegmentSourceId | None:
    """Parse current and legacy ScreenPipe segment IDs without inventing a track.

    Current IDs are ``screenpipe:source:direction:track:first-last``. Track IDs
    may themselves contain colons, so parsing works inwards from the stable
    prefix and final range. Legacy IDs are
    ``screenpipe:source:direction:first-last`` and return ``track_id=None``. The
    verified oldest shape, ``screenpipe:source:numeric-numeric``, additionally
    returns ``direction='unknown'``. Non-ScreenPipe IDs return ``None``;
    malformed ScreenPipe IDs fail loudly.
    """

    external = str(external_source_id or "")
    if not external.startswith("screenpipe:"):
        return None
    parts = external.split(":")
    if len(parts) == 3:
        first, separator, last = parts[2].partition("-")
        if separator and first.isdigit() and last.isdigit():
            return ScreenPipeSegmentSourceId(
                capture_source_id=_source_id_component(parts[1], "capture source id"),
                direction="unknown",
                track_id=None,
                source_range=parts[2],
            )
    if len(parts) < 4:
        raise ValueError(f"malformed ScreenPipe external source id: {external!r}")
    source = _source_id_component(parts[1], "capture source id")
    direction = _source_id_component(parts[2], "direction")
    source_range = _source_id_component(parts[-1], "source range")
    track = None
    if len(parts) >= 5:
        track = _source_id_component(
            ":".join(parts[3:-1]), "track id", allow_colon=True
        )
    return ScreenPipeSegmentSourceId(
        capture_source_id=source,
        direction=direction,
        track_id=track,
        source_range=source_range,
    )


def transcript_evidence_locator(
    external_source_id: str | None,
    client_id: str | None,
    conversation_id: str,
    direction: str,
) -> EvidenceLocator:
    """Resolve a stable transcript lane from its exact persisted source identity."""

    parsed = parse_screenpipe_segment_source_id(external_source_id)
    if parsed is not None:
        return EvidenceLocator(
            capture_source_id=parsed.capture_source_id,
            modality="transcript",
            track_id=parsed.track_id,
        )
    return EvidenceLocator(
        capture_source_id=str(client_id or f"conversation:{conversation_id}"),
        modality="transcript",
        track_id=direction,
    )
