import io
import json
import os
from dataclasses import asdict
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image

from backend.models.device_input import DeviceInputItem
from backend.services.timeline.immich_visual_evidence import (
    ConfiguredImmichVisualAnalyzer,
    ImmichThumbnail,
    ImmichVisualEvidencePreparer,
    ImmichVisualObservation,
)


@pytest.fixture
async def visual_db(mongo_service, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27018"))
    database = client["test_immich_visual_evidence_db"]
    await init_beanie(database=database, document_models=[DeviceInputItem])
    yield
    await client.drop_database("test_immich_visual_evidence_db")
    client.close()


async def insert_photo(asset_id: str, hour: int) -> DeviceInputItem:
    row = DeviceInputItem(
        user_id="user",
        source_id="immich-default",
        kind="immich_memory",
        source_item_id=asset_id,
        locator={
            "capture_source_id": "immich-default",
            "modality": "photo",
            "track_id": None,
        },
        captured_at=datetime(2026, 8, 15, hour, tzinfo=timezone.utc),
        metadata={"asset_id": asset_id, "filename": f"{asset_id}.HEIC"},
    )
    await row.insert()
    return row


def _pixels():
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(out, format="PNG")
    return out.getvalue()


class ThumbnailProvider:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    async def fetch_many(self, assets, *, size="thumbnail"):
        self.calls.append(list(assets))
        return (
            [
                ImmichThumbnail(
                    asset_id, f"immich-{asset_id}.jpg", _pixels(), "image/png"
                )
                for asset_id, _filename in assets
                if asset_id not in self.failures
            ],
            {
                asset_id: self.failures[asset_id]
                for asset_id, _ in assets
                if asset_id in self.failures
            },
        )


class Analyzer:
    identity = "fake-vision-v1"

    def decode(self, raw):
        return ConfiguredImmichVisualAnalyzer({}).decode(raw)

    def __init__(self):
        self.calls = []

    async def analyze(self, images, context):
        self.calls.append((list(images), context))
        metadata = json.loads(context[context.index("\n{") + 1 :])["tile_metadata"]
        images = [SimpleNamespace(asset_id=x["asset_id"]) for x in metadata]
        observations = [
            ImmichVisualObservation(
                asset_id=image.asset_id,
                description=(
                    "A brown dog playing with a red ball in a garden."
                    if image.asset_id == "dog"
                    else "A dark, blurred pocket photo with no visible activity."
                ),
                ocr_text="",
                entities=["dog", "red ball"] if image.asset_id == "dog" else [],
                activities=["playing"] if image.asset_id == "dog" else [],
                setting="garden" if image.asset_id == "dog" else "unknown",
                timeline_relevance="high" if image.asset_id == "dog" else "none",
                relevance_reason=(
                    "Distinct pet activity can corroborate nearby conversation."
                    if image.asset_id == "dog"
                    else "No reconstructable visual content."
                ),
            )
            for image in images
        ]
        return {
            "images": [asdict(x) for x in observations],
            "request": {
                "action": "finish",
                "question": "",
                "query": "",
                "asset_ids": [],
                "person_ids": [],
                "started_at": "",
                "ended_at": "",
            },
        }


@pytest.mark.asyncio
async def test_prepares_bounded_text_without_persisting_pixels(visual_db):
    await insert_photo("dog", 8)
    await insert_photo("blur", 9)
    provider = ThumbnailProvider()
    analyzer = Analyzer()
    preparer = ImmichVisualEvidencePreparer(provider, analyzer)

    result = await preparer.prepare_day(
        "user",
        date(2026, 8, 15),
        "UTC",
        cutoff=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert result.state == "complete"
    assert result.candidate_count == 2
    assert result.analyzed_count == 2
    assert result.helpful_count == 1
    assert result.unhelpful_count == 1
    dog = await DeviceInputItem.find_one(DeviceInputItem.source_item_id == "dog")
    assert dog is not None
    assert dog.metadata["description"].startswith("A brown dog")
    assert dog.metadata["entities"] == ["dog", "red ball"]
    assert dog.metadata["timeline_relevance"] == "high"
    assert dog.metadata["visual_analysis"]["state"] == "complete"
    assert dog.content_hash
    assert dog.media_data is None

    again = await preparer.prepare_day(
        "user",
        date(2026, 8, 15),
        "UTC",
        cutoff=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert again.analyzed_count == 2
    assert again.newly_analyzed_count == 0
    assert len(provider.calls[-1]) == 2
    assert len(analyzer.calls) == 1


@pytest.mark.asyncio
async def test_partial_thumbnail_failure_keeps_usable_visual_evidence(visual_db):
    await insert_photo("dog", 8)
    await insert_photo("missing", 9)
    preparer = ImmichVisualEvidencePreparer(
        ThumbnailProvider({"missing": "Immich returned 404"}), Analyzer()
    )

    result = await preparer.prepare_day(
        "user",
        date(2026, 8, 15),
        "UTC",
        cutoff=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert result.state == "partial"
    assert result.analyzed_count == 1
    assert result.helpful_count == 1
    assert result.failed_count == 1
    missing = await DeviceInputItem.find_one(
        DeviceInputItem.source_item_id == "missing"
    )
    assert missing is not None
    assert missing.metadata["visual_analysis"]["state"] == "failed"
    assert missing.metadata["visual_analysis"]["attempts"] == 1


@pytest.mark.asyncio
async def test_all_thumbnail_failures_are_fail_closed(visual_db):
    await insert_photo("missing", 8)
    preparer = ImmichVisualEvidencePreparer(
        ThumbnailProvider({"missing": "Immich returned 404"}), Analyzer()
    )

    result = await preparer.prepare_day(
        "user",
        date(2026, 8, 15),
        "UTC",
        cutoff=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert result.state == "failed"
    assert result.analyzed_count == 0
    assert result.failed_count == 1


@pytest.mark.asyncio
async def test_model_route_change_reanalyzes_cached_description(visual_db):
    row = await insert_photo("dog", 8)
    row.metadata["description"] = "old cloud description"
    row.metadata["visual_analysis"] = {
        "state": "complete",
        "version": "immich-timeline-vision-v1:codex:gpt-5.6-luna",
    }
    await row.save()
    provider = ThumbnailProvider()
    analyzer = Analyzer()
    preparer = ImmichVisualEvidencePreparer(
        provider,
        analyzer,
        analysis_version=(
            "immich-timeline-vision-v1:model:immich_visual_evidence:"
            "llamacpp:qwen3.8-llm:qwen3.8"
        ),
    )

    result = await preparer.prepare_day(
        "user",
        date(2026, 8, 15),
        "UTC",
        cutoff=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert result.newly_analyzed_count == 1
    refreshed = await DeviceInputItem.get(row.id)
    assert refreshed is not None
    assert refreshed.metadata["description"].startswith("A brown dog")
    assert refreshed.metadata["visual_analysis"]["version"].endswith(
        "llamacpp:qwen3.8-llm:qwen3.8"
    )


@pytest.mark.asyncio
async def test_later_round_failure_preserves_completed_database_observations(visual_db):
    await insert_photo("dog", 8)
    await insert_photo("blur", 9)

    class LaterFailure(Analyzer):
        async def analyze(self, images, context):
            raw = await super().analyze(images, context)
            raw["request"].update(
                action="search", query="garden", question="Check another garden scene"
            )
            return raw

    class FailedSearch(ThumbnailProvider):
        async def search(self, *_args, **_kwargs):
            raise ValueError("search failed after successful first round")

    preparer = ImmichVisualEvidencePreparer(FailedSearch(), LaterFailure())
    with pytest.raises(ValueError, match="search failed"):
        await preparer.prepare_day(
            "user",
            date(2026, 8, 15),
            "UTC",
            cutoff=datetime(2026, 8, 16, tzinfo=timezone.utc),
        )
    rows = await DeviceInputItem.find(DeviceInputItem.user_id == "user").to_list()
    assert len(rows) == 2
    assert all(row.metadata["visual_analysis"]["state"] == "complete" for row in rows)


@pytest.mark.asyncio
async def test_visual_preparation_does_not_cross_fixed_cutoff(visual_db):
    await insert_photo("dog", 8)
    await insert_photo("blur", 9)
    preparer = ImmichVisualEvidencePreparer(ThumbnailProvider(), Analyzer())
    result = await preparer.prepare_day(
        "user",
        date(2026, 8, 15),
        "UTC",
        cutoff=datetime(2026, 8, 15, 9, tzinfo=timezone.utc),
    )
    assert result.candidate_count == result.analyzed_count == 1
    untouched = await DeviceInputItem.find_one(DeviceInputItem.source_item_id == "blur")
    assert "visual_analysis" not in untouched.metadata
