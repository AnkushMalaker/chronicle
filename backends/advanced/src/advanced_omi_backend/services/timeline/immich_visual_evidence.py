"""Bounded visual understanding for Immich evidence used by Timeline.

The phone remains out of this path. Immich owns upload; after the explicit readiness
gate succeeds, this module fetches small server-side thumbnails, describes them once,
and persists only bounded semantic metadata on the existing ``DeviceInputItem``.
Timeline callers see one deep interface: prepare one local day's visual evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import httpx

from advanced_omi_backend.config_loader import load_config
from advanced_omi_backend.models.device_input import DeviceInputItem, utcnow
from advanced_omi_backend.services.vision import (
    VisionError,
    VisionUnavailable,
    run_structured_vision,
    structured_vision_settings,
    vision_route_identity,
)

from .executor import settings_dict

logger = logging.getLogger(__name__)

VISUAL_EVIDENCE_VERSION = "immich-timeline-vision-v1"
MAX_ANALYSIS_ATTEMPTS = 3
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_ASSETS = 12
_FETCH_CONCURRENCY = 4
_HELPFUL_RELEVANCE = {"high", "medium"}

TimelineRelevance = Literal["high", "medium", "low", "none"]
PreparationState = Literal["not_needed", "complete", "partial", "failed"]


@dataclass(frozen=True)
class ImmichThumbnail:
    asset_id: str
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class ImmichVisualObservation:
    asset_id: str
    description: str
    ocr_text: str
    entities: list[str]
    activities: list[str]
    setting: str
    timeline_relevance: TimelineRelevance
    relevance_reason: str


@dataclass(frozen=True)
class ImmichVisualPreparation:
    state: PreparationState
    candidate_count: int
    analyzed_count: int
    newly_analyzed_count: int
    helpful_count: int
    unhelpful_count: int
    failed_count: int

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class ImmichThumbnailProvider(Protocol):
    async def fetch_many(
        self, assets: Sequence[tuple[str, str]]
    ) -> tuple[list[ImmichThumbnail], dict[str, str]]: ...


class ImmichVisualAnalyzer(Protocol):
    async def analyze(
        self,
        images: Sequence[ImmichThumbnail],
        context: Mapping[str, Mapping[str, str]],
    ) -> list[ImmichVisualObservation]: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day_bounds(local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(
        local_date + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return start, end


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _bounded_list(value: Any, *, items: int = 20, chars: int = 120) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _bounded(item, chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= items:
            break
    return result


def immich_visual_settings(settings: Any = None) -> dict[str, Any]:
    """Validated settings for the one bounded visual pass per explicit day."""

    codex_settings: dict[str, Any] = {}
    if settings is None:
        settings = settings_dict().get("immich_visual_evidence") or {}
        config = load_config()
        codex_settings = ((config.get("vision") or {}).get("backends") or {}).get(
            "codex", {}
        )
    if not isinstance(settings, dict):
        raise ValueError("timeline.immich_visual_evidence must be a mapping")
    maximum = int(settings.get("max_assets_per_day", _DEFAULT_MAX_ASSETS))
    if maximum <= 0 or maximum > _DEFAULT_MAX_ASSETS:
        raise ValueError(
            f"timeline.immich_visual_evidence.max_assets_per_day must be 1-{_DEFAULT_MAX_ASSETS}"
        )
    return {
        "max_assets_per_day": maximum,
        "vision": structured_vision_settings(
            settings,
            label="timeline.immich_visual_evidence",
            default_operation="immich_visual_evidence",
            codex_settings=codex_settings,
        ),
    }


class ImmichHttpThumbnailProvider:
    """Production adapter for Immich's authenticated server-side thumbnails."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.getenv("IMMICH_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("IMMICH_API_KEY", "")
        if not self.base_url or not self.api_key:
            raise RuntimeError("Immich is not configured for visual evidence")

    async def fetch_many(
        self, assets: Sequence[tuple[str, str]]
    ) -> tuple[list[ImmichThumbnail], dict[str, str]]:
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
        results: list[ImmichThumbnail] = []
        failures: dict[str, str] = {}

        async with httpx.AsyncClient(
            timeout=60,
            headers={"x-api-key": self.api_key, "Accept": "image/*"},
        ) as client:

            async def fetch(asset_id: str, original_filename: str) -> None:
                try:
                    async with semaphore:
                        response = await client.get(
                            f"{self.base_url}/api/assets/{asset_id}/thumbnail",
                            params={"size": "thumbnail"},
                        )
                    response.raise_for_status()
                    if len(response.content) > MAX_THUMBNAIL_BYTES:
                        raise ValueError("thumbnail exceeds the visual evidence limit")
                    content_type = response.headers.get(
                        "content-type", "image/jpeg"
                    ).split(";", 1)[0]
                    if not content_type.startswith("image/"):
                        raise ValueError("Immich returned a non-image thumbnail")
                    results.append(
                        ImmichThumbnail(
                            asset_id=asset_id,
                            filename=_vision_filename(
                                asset_id, original_filename, content_type
                            ),
                            content=response.content,
                            content_type=content_type,
                        )
                    )
                except (httpx.HTTPError, ValueError) as error:
                    failures[asset_id] = f"{type(error).__name__}: {error}"[:500]

            await asyncio.gather(*(fetch(*asset) for asset in assets))
        order = {asset_id: index for index, (asset_id, _name) in enumerate(assets)}
        results.sort(key=lambda item: order[item.asset_id])
        return results, failures


def _vision_filename(asset_id: str, original: str, content_type: str) -> str:
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }.get(content_type.lower())
    if suffix is None:
        suffix = Path(original).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        suffix = ".jpg"
    return f"immich-{asset_id}{suffix}"


_OBSERVATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "asset_id": {"type": "string"},
                    "description": {"type": "string"},
                    "ocr_text": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "activities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "setting": {"type": "string"},
                    "timeline_relevance": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "none"],
                    },
                    "relevance_reason": {"type": "string"},
                },
                "required": [
                    "asset_id",
                    "description",
                    "ocr_text",
                    "entities",
                    "activities",
                    "setting",
                    "timeline_relevance",
                    "relevance_reason",
                ],
            },
        }
    },
    "required": ["images"],
}

_PROMPT = """\
Describe each Immich photo as factual timeline evidence. The goal is to help reconstruct
what the user was doing around the capture time and connect it with conversations,
screen activity, and other evidence.

For each image:
- Return its exact asset_id from the supplied context.
- Describe visible people generically; never infer or guess identity.
- Name visible animals, objects, activities, food, places, travel, events, and readable
  text that could explain the surrounding time window.
- Transcribe useful visible text into ocr_text; do not invent obscured text.
- Rate timeline_relevance: high for a distinctive event/activity/place/interaction,
  medium for useful situational context, low for an ordinary but usable moment, and
  none only when the image is blank, corrupt, or visually uninformative.
- Keep every field concise and observational. Do not add memories or conclusions that
  are not visible in the image.
"""


class ConfiguredImmichVisualAnalyzer:
    """Production adapter over Chronicle's selected structured-vision route."""

    def __init__(self, settings: Mapping[str, Any]):
        self.settings = settings

    async def analyze(
        self,
        images: Sequence[ImmichThumbnail],
        context: Mapping[str, Mapping[str, str]],
    ) -> list[ImmichVisualObservation]:
        prompt = f"{_PROMPT}\n\nImage context:\n{dict(context)}"
        result = await run_structured_vision(
            prompt,
            [(item.filename, item.content) for item in images],
            _OBSERVATION_SCHEMA,
            self.settings,
        )
        observations: list[ImmichVisualObservation] = []
        for row in result.get("images") or []:
            relevance = str(row.get("timeline_relevance") or "none")
            if relevance not in {"high", "medium", "low", "none"}:
                relevance = "none"
            observations.append(
                ImmichVisualObservation(
                    asset_id=_bounded(row.get("asset_id"), 100),
                    description=_bounded(row.get("description"), 1200),
                    ocr_text=_bounded(row.get("ocr_text"), 2000),
                    entities=_bounded_list(row.get("entities")),
                    activities=_bounded_list(row.get("activities")),
                    setting=_bounded(row.get("setting"), 300),
                    timeline_relevance=relevance,  # type: ignore[arg-type]
                    relevance_reason=_bounded(row.get("relevance_reason"), 500),
                )
            )
        return observations


class ImmichVisualEvidencePreparer:
    """Deep module that owns selection state, adapters, analysis, and persistence."""

    def __init__(
        self,
        thumbnail_provider: ImmichThumbnailProvider,
        analyzer: ImmichVisualAnalyzer,
        *,
        max_assets_per_day: int = _DEFAULT_MAX_ASSETS,
        timeout_seconds: int = 600,
        analysis_version: str = VISUAL_EVIDENCE_VERSION,
    ):
        self.thumbnail_provider = thumbnail_provider
        self.analyzer = analyzer
        self.max_assets_per_day = max_assets_per_day
        self.timeout_seconds = timeout_seconds
        self.analysis_version = analysis_version

    async def prepare_day(
        self, user_id: str, local_date: date, timezone_name: str
    ) -> ImmichVisualPreparation:
        start, end = _day_bounds(local_date, timezone_name)
        rows = (
            await DeviceInputItem.find(
                DeviceInputItem.user_id == user_id,
                DeviceInputItem.kind == "immich_memory",
                DeviceInputItem.captured_at >= start,
                DeviceInputItem.captured_at < end,
            )
            .sort("+captured_at")
            .limit(self.max_assets_per_day)
            .to_list()
        )
        if not rows:
            return ImmichVisualPreparation("not_needed", 0, 0, 0, 0, 0, 0)

        complete = [row for row in rows if _visual_complete(row, self.analysis_version)]
        claimed: list[DeviceInputItem] = []
        for row in rows:
            if row in complete:
                continue
            if await self._claim(row):
                claimed.append(row)

        fetch_assets = [
            (
                str(row.metadata.get("asset_id") or row.source_item_id),
                str(row.metadata.get("filename") or "photo.jpg"),
            )
            for row in claimed
        ]
        thumbnails, fetch_failures = await self.thumbnail_provider.fetch_many(
            fetch_assets
        )
        rows_by_asset = {
            str(row.metadata.get("asset_id") or row.source_item_id): row
            for row in claimed
        }
        for asset_id, error in fetch_failures.items():
            row = rows_by_asset.get(asset_id)
            if row is not None:
                await self._fail(row, error)

        newly_analyzed = 0
        if thumbnails:
            context = {
                item.asset_id: {
                    "asset_id": item.asset_id,
                    "filename": str(
                        rows_by_asset[item.asset_id].metadata.get("filename") or ""
                    ),
                    "captured_at": _as_utc(
                        rows_by_asset[item.asset_id].captured_at
                    ).isoformat(),
                }
                for item in thumbnails
            }
            try:
                observations = await self.analyzer.analyze(thumbnails, context)
            except VisionUnavailable:
                for thumbnail in thumbnails:
                    await self._release(rows_by_asset[thumbnail.asset_id])
                raise
            except (VisionError, OSError, ValueError) as error:
                for thumbnail in thumbnails:
                    await self._fail(
                        rows_by_asset[thumbnail.asset_id],
                        f"{type(error).__name__}: {error}"[:500],
                    )
            else:
                by_asset = {item.asset_id: item for item in observations}
                offered = {item.asset_id for item in thumbnails}
                for asset_id in offered:
                    row = rows_by_asset[asset_id]
                    observation = by_asset.get(asset_id)
                    if observation is None:
                        await self._fail(row, "vision result omitted this asset")
                        continue
                    thumbnail = next(
                        item for item in thumbnails if item.asset_id == asset_id
                    )
                    await self._complete(row, thumbnail, observation)
                    newly_analyzed += 1

        refreshed = await DeviceInputItem.find(
            {"_id": {"$in": [row.id for row in rows]}}
        ).to_list()
        analyzed = [
            row for row in refreshed if _visual_complete(row, self.analysis_version)
        ]
        helpful = [
            row
            for row in analyzed
            if (row.metadata.get("visual_analysis") or {}).get("timeline_relevance")
            in _HELPFUL_RELEVANCE
        ]
        failed = [
            row
            for row in refreshed
            if (row.metadata.get("visual_analysis") or {}).get("state") == "failed"
        ]
        if failed and not analyzed:
            state: PreparationState = "failed"
        elif failed:
            state = "partial"
        else:
            state = "complete"
        return ImmichVisualPreparation(
            state=state,
            candidate_count=len(rows),
            analyzed_count=len(analyzed),
            newly_analyzed_count=newly_analyzed,
            helpful_count=len(helpful),
            unhelpful_count=len(analyzed) - len(helpful),
            failed_count=len(failed),
        )

    async def _claim(self, row: DeviceInputItem) -> bool:
        now = utcnow()
        stale_before = now - timedelta(seconds=2 * self.timeout_seconds)
        result = await DeviceInputItem.get_pymongo_collection().update_one(
            {
                "_id": row.id,
                "$or": [
                    {"metadata.visual_analysis": {"$exists": False}},
                    {
                        "metadata.visual_analysis.version": {
                            "$ne": self.analysis_version
                        }
                    },
                    {
                        "metadata.visual_analysis.state": {
                            "$in": ["pending", "failed"]
                        },
                        "metadata.visual_analysis.attempts": {
                            "$lt": MAX_ANALYSIS_ATTEMPTS
                        },
                    },
                    {"metadata.visual_analysis.started_at": {"$lt": stale_before}},
                ],
            },
            {
                "$set": {
                    "metadata.visual_analysis.state": "processing",
                    "metadata.visual_analysis.version": self.analysis_version,
                    "metadata.visual_analysis.started_at": now,
                },
                "$setOnInsert": {"metadata.visual_analysis.attempts": 0},
            },
        )
        return result.modified_count == 1

    async def _release(self, row: DeviceInputItem) -> None:
        await DeviceInputItem.get_pymongo_collection().update_one(
            {"_id": row.id},
            {
                "$set": {"metadata.visual_analysis.state": "pending"},
                "$unset": {"metadata.visual_analysis.started_at": ""},
            },
        )

    async def _fail(self, row: DeviceInputItem, error: str) -> None:
        await DeviceInputItem.get_pymongo_collection().update_one(
            {"_id": row.id},
            {
                "$set": {
                    "metadata.visual_analysis.state": "failed",
                    "metadata.visual_analysis.version": self.analysis_version,
                    "metadata.visual_analysis.error": error[:500],
                    "metadata.visual_analysis.completed_at": utcnow(),
                },
                "$inc": {"metadata.visual_analysis.attempts": 1},
                "$unset": {"metadata.visual_analysis.started_at": ""},
            },
        )

    async def _complete(
        self,
        row: DeviceInputItem,
        thumbnail: ImmichThumbnail,
        observation: ImmichVisualObservation,
    ) -> None:
        now = utcnow()
        visual = {
            "state": "complete",
            "version": self.analysis_version,
            "timeline_relevance": observation.timeline_relevance,
            "relevance_reason": observation.relevance_reason,
            "analyzed_at": now,
            "attempts": int(
                (row.metadata.get("visual_analysis") or {}).get("attempts") or 0
            )
            + 1,
        }
        digest = hashlib.sha256(thumbnail.content).hexdigest()
        await DeviceInputItem.get_pymongo_collection().update_one(
            {"_id": row.id},
            {
                "$set": {
                    "metadata.description": observation.description,
                    "metadata.ocr_text": observation.ocr_text,
                    "metadata.entities": observation.entities,
                    "metadata.activities": observation.activities,
                    "metadata.setting": observation.setting,
                    "metadata.timeline_relevance": observation.timeline_relevance,
                    "metadata.relevance_reason": observation.relevance_reason,
                    "metadata.visual_analysis": visual,
                    "content_hash": digest,
                    "curation_revision": f"{self.analysis_version}:{digest}",
                    "curated_at": now,
                }
            },
        )


def _visual_complete(
    row: DeviceInputItem, analysis_version: str = VISUAL_EVIDENCE_VERSION
) -> bool:
    state = row.metadata.get("visual_analysis") or {}
    return (
        state.get("state") == "complete"
        and state.get("version") == analysis_version
        and bool(str(row.metadata.get("description") or "").strip())
    )


def build_immich_visual_evidence_preparer() -> ImmichVisualEvidencePreparer:
    settings = immich_visual_settings()
    analysis_version = (
        f"{VISUAL_EVIDENCE_VERSION}:{vision_route_identity(settings['vision'])}"
    )
    return ImmichVisualEvidencePreparer(
        ImmichHttpThumbnailProvider(),
        ConfiguredImmichVisualAnalyzer(settings["vision"]),
        max_assets_per_day=int(settings["max_assets_per_day"]),
        timeout_seconds=int(settings["vision"]["timeout_seconds"]),
        analysis_version=analysis_version,
    )


async def prepare_immich_visual_evidence(
    user_id: str, local_date: date, timezone_name: str
) -> ImmichVisualPreparation:
    """Prepare one explicit day through the production adapters."""

    return await build_immich_visual_evidence_preparer().prepare_day(
        user_id, local_date, timezone_name
    )
