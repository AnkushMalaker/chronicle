"""Bounded visual understanding for Immich evidence used by Timeline.

The phone remains out of this path. Immich owns upload; after the explicit readiness
gate succeeds, this module fetches small server-side thumbnails, describes them once,
and persists only bounded semantic metadata on the existing ``DeviceInputItem``.
Timeline callers see one deep interface: prepare one local day's visual evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import httpx
import jsonschema

from backend.config_loader import load_config
from backend.models.device_input import DeviceInputItem, utcnow
from backend.services.job_progress import report_job_progress
from backend.services.vision import (
    VisionError,
    VisionUnavailable,
    run_structured_vision,
    structured_vision_settings,
    vision_route_identity,
)

from . import photo_exploration
from .executor import settings_dict

logger = logging.getLogger(__name__)

VISUAL_EVIDENCE_VERSION = "immich-photo-exploration-v2"
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
    uninspected_count: int = 0
    exploration_rounds: int = 0
    stop_reason: str = ""
    artifact_id: str = ""

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class ImmichThumbnailProvider(Protocol):
    async def fetch_many(
        self, assets: Sequence[tuple[str, str]], *, size: str = "thumbnail"
    ) -> tuple[list[ImmichThumbnail], dict[str, str]]: ...


class ImmichVisualAnalyzer(Protocol):
    @property
    def identity(self) -> str: ...

    async def analyze(
        self, images: Sequence[tuple[str, bytes]], prompt: str
    ) -> dict: ...

    def decode(self, result: dict) -> Any: ...


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
        "max_rounds": int(settings.get("max_rounds", 4)),
        "max_image_views": int(settings.get("max_image_views", 48)),
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
        self, assets: Sequence[tuple[str, str]], *, size: str = "thumbnail"
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
                            params={"size": size},
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

    async def search(
        self,
        query: str,
        *,
        allowed_ids: set[str],
        started_at: datetime,
        ended_at: datetime,
    ) -> list[str]:
        """Bound remote relevance ranking; immutable inventory membership wins."""
        found = []
        async with httpx.AsyncClient(
            timeout=30, headers={"x-api-key": self.api_key}
        ) as client:
            for page in range(1, 5):
                response = await client.post(
                    f"{self.base_url}/api/search/smart",
                    json={
                        "query": query,
                        "type": "IMAGE",
                        "page": page,
                        "size": 100,
                        "takenAfter": (started_at - timedelta(days=1)).isoformat(),
                        "takenBefore": (ended_at + timedelta(days=1)).isoformat(),
                    },
                )
                response.raise_for_status()
                assets = response.json().get("assets", {})
                found.extend(
                    str(x["id"])
                    for x in assets.get("items", [])
                    if str(x["id"]) in allowed_ids
                )
                if assets.get("nextPage") is None or len(found) >= 48:
                    break
        return list(dict.fromkeys(found))[:48]


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


class ConfiguredImmichVisualAnalyzer:
    """Production adapter over Chronicle's selected structured-vision route."""

    def __init__(self, settings: Mapping[str, Any]):
        self.settings = settings

    @property
    def identity(self):
        return f"{VISUAL_EVIDENCE_VERSION}:{vision_route_identity(self.settings)}"

    @staticmethod
    def schema():
        schema = json.loads(json.dumps(_OBSERVATION_SCHEMA))
        schema["properties"]["request"] = photo_exploration.REQUEST_SCHEMA
        schema["required"].append("request")
        return schema

    async def analyze(self, images, prompt):
        return await run_structured_vision(prompt, images, self.schema(), self.settings)

    def decode(self, result):
        try:
            jsonschema.validate(result, self.schema())
        except jsonschema.ValidationError as error:
            raise ValueError("Invalid photo exploration response") from error
        observations = []
        for row in result.get("images") or []:
            observations.append(
                ImmichVisualObservation(
                    asset_id=_bounded(row.get("asset_id"), 100),
                    description=_bounded(row.get("description"), 1200),
                    ocr_text=_bounded(row.get("ocr_text"), 2000),
                    entities=_bounded_list(row.get("entities")),
                    activities=_bounded_list(row.get("activities")),
                    setting=_bounded(row.get("setting"), 300),
                    timeline_relevance=row["timeline_relevance"],
                    relevance_reason=_bounded(row.get("relevance_reason"), 500),
                )
            )
        return photo_exploration.PhotoRound(observations, result["request"])


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
        max_rounds: int = 4,
        max_image_views: int = 48,
        artifact_root: Path = Path("data/timeline_photo_exploration"),
    ):
        self.thumbnail_provider = thumbnail_provider
        self.analyzer = analyzer
        self.max_assets_per_day = max_assets_per_day
        self.timeout_seconds = timeout_seconds
        self.analysis_version = analysis_version
        self.max_rounds, self.max_image_views, self.artifact_root = (
            max_rounds,
            max_image_views,
            artifact_root,
        )

    async def prepare_day(
        self, user_id: str, local_date: date, timezone_name: str, *, cutoff: datetime
    ) -> ImmichVisualPreparation:
        start, end = _day_bounds(local_date, timezone_name)
        end = min(end, _as_utc(cutoff))
        rows = (
            await DeviceInputItem.find(
                DeviceInputItem.user_id == user_id,
                DeviceInputItem.kind == "immich_memory",
                DeviceInputItem.captured_at >= start,
                DeviceInputItem.captured_at < end,
            )
            .sort("+captured_at")
            .to_list()
        )
        if not rows:
            return ImmichVisualPreparation("not_needed", 0, 0, 0, 0, 0, 0)

        rows_by_asset = {row.source_item_id: row for row in rows}
        catalog = [
            {
                **(row.metadata.get("photo_metadata") or {}),
                "asset_id": row.source_item_id,
                "filename": row.metadata.get("filename") or "photo.jpg",
                "captured_at": _as_utc(row.captured_at).isoformat(),
            }
            for row in rows
        ]
        identity = hashlib.sha256(
            json.dumps(
                [
                    catalog,
                    self.analysis_version,
                    timezone_name,
                    end.isoformat(),
                    self.max_assets_per_day,
                    self.max_rounds,
                    self.max_image_views,
                    self.analyzer.identity,
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        artifact_dir = self.artifact_root / user_id / local_date.isoformat() / identity
        claimed = set()

        async def acquire(asset_id):
            row = rows_by_asset[asset_id]
            if asset_id in claimed or _visual_complete(row, self.analysis_version):
                return True
            if await self._claim(row):
                claimed.add(asset_id)
                return True
            return False

        explorer = photo_exploration.PhotoExplorer(
            self.thumbnail_provider,
            self.analyzer,
            overview_size=self.max_assets_per_day,
            max_rounds=self.max_rounds,
            max_image_views=self.max_image_views,
        )
        newly_analyzed = 0
        inspected_ids = set()

        async def persist_round(observations, images, failures):
            nonlocal newly_analyzed
            inspected_ids.update(observations)
            await report_job_progress(
                "photos",
                "Photo exploration updated",
                completed=len(inspected_ids),
                total=len(rows_by_asset),
                unit="photos",
            )
            for asset_id, error in failures.items():
                if asset_id in claimed:
                    await self._fail(rows_by_asset[asset_id], error)
                    claimed.discard(asset_id)
            for asset_id, observation in observations.items():
                row = rows_by_asset[asset_id]
                image = images[asset_id]
                if (
                    not _visual_complete(row, self.analysis_version)
                    or row.content_hash != hashlib.sha256(image.content).hexdigest()
                    or any(
                        row.metadata.get(key) != getattr(observation, key)
                        for key in (
                            "description",
                            "ocr_text",
                            "entities",
                            "activities",
                            "setting",
                            "timeline_relevance",
                            "relevance_reason",
                        )
                    )
                ):
                    await self._complete(row, image, observation)
                    newly_analyzed += 1
                    rows_by_asset[asset_id] = await DeviceInputItem.get(row.id)
                claimed.discard(asset_id)

        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await explorer.explore(
                    catalog,
                    timezone_name,
                    artifact_dir=artifact_dir,
                    acquire=acquire,
                    on_round=persist_round,
                )
            for asset_id, error in result.failures.items():
                if asset_id in claimed:
                    await self._fail(rows_by_asset[asset_id], error)
                    claimed.discard(asset_id)
        except VisionUnavailable:
            for asset_id in claimed:
                await self._release(rows_by_asset[asset_id])
            raise
        except (
            VisionError,
            OSError,
            ValueError,
            TimeoutError,
            httpx.HTTPError,
        ) as error:
            for asset_id in claimed:
                await self._fail(rows_by_asset[asset_id], str(error))
            raise

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
        elif failed or len(analyzed) < len(rows):
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
            uninspected_count=len(rows) - len(analyzed),
            exploration_rounds=len(result.rounds),
            stop_reason=result.stop_reason,
            artifact_id=identity,
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
                    "curation_revision": f"{self.analysis_version}:{hashlib.sha256(json.dumps([digest, observation.__dict__], sort_keys=True).encode()).hexdigest()}",
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
        max_rounds=settings["max_rounds"],
        max_image_views=settings["max_image_views"],
    )


async def prepare_immich_visual_evidence(
    user_id: str, local_date: date, timezone_name: str, *, cutoff: datetime
) -> ImmichVisualPreparation:
    """Prepare one explicit day through the production adapters."""

    return await build_immich_visual_evidence_preparer().prepare_day(
        user_id, local_date, timezone_name, cutoff=cutoff
    )
