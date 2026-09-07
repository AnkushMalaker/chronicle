import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from backend.services.timeline.immich_visual_evidence import (
    ConfiguredImmichVisualAnalyzer,
    ImmichThumbnail,
)
from backend.services.timeline.photo_exploration import PhotoExplorer
from backend.services.timeline.photo_sampling import sample_photos


def catalog(count=30):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "asset_id": f"a{i:03d}",
            "captured_at": (start + timedelta(minutes=i)).isoformat(),
            "filename": f"{i}.jpg",
            "people": [],
        }
        for i in range(count)
    ]


def request(action="finish", **kwargs):
    return {
        "action": action,
        "question": "Resolve activity in the unexplored interval",
        "query": "",
        "asset_ids": [],
        "person_ids": [],
        "started_at": "",
        "ended_at": "",
        **kwargs,
    }


class Provider:
    def __init__(self):
        self.calls = []
        self.search_calls = []

    async def fetch_many(self, assets, *, size="thumbnail"):
        self.calls.append((assets, size))
        out = io.BytesIO()
        Image.new("RGB", (40, 30), "blue").save(out, format="PNG")
        return [
            ImmichThumbnail(aid, name, out.getvalue(), "image/png")
            for aid, name in assets
        ], {}

    async def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        return ["a005", "a025", "outside-inventory"]


class Analyzer:
    identity = "scripted-v1"

    def __init__(self, *requests):
        self.requests = list(requests)
        self.calls = []

    def decode(self, raw):
        return ConfiguredImmichVisualAnalyzer({}).decode(raw)

    async def analyze(self, images, prompt):
        metadata = json.loads(prompt[prompt.index("\n{") + 1 :])
        self.calls.append((images, metadata))
        return {
            "images": [
                {
                    "asset_id": m["asset_id"],
                    "description": "A visible scene",
                    "ocr_text": "",
                    "entities": [],
                    "activities": [],
                    "setting": "",
                    "timeline_relevance": "medium",
                    "relevance_reason": "Observational scene",
                }
                for m in metadata["tile_metadata"]
            ],
            "request": self.requests.pop(0) if self.requests else request(),
        }


def test_sampling_covers_late_day_and_preserves_duplicate_membership():
    rows = catalog(100)
    rows[20]["duplicate_id"] = rows[21]["duplicate_id"] = "same"
    sampled = sample_photos(rows)
    assert sampled[0]["asset_id"] == "a000" and sampled[-1]["asset_id"] == "a099"
    assert len(sampled) == 12 and len(rows) == 100
    assert sum(x.get("duplicate_id") == "same" for x in sampled) <= 1


@pytest.mark.asyncio
async def test_recursive_time_sampling_uses_unseen_members_and_retains_inputs(tmp_path):
    p = Provider()
    a = Analyzer(request("sample", started_at="2026-01-01T00:20:00Z"))
    result = await PhotoExplorer(p, a, overview_size=4).explore(
        catalog(), "Asia/Kolkata", artifact_dir=tmp_path
    )
    first, second = [set(x["offered"]) for x in result.rounds]
    assert not first & second and all(int(x[1:]) >= 20 for x in second)
    assert (tmp_path / "round-01.png").exists()
    assert "not the whole library" in (tmp_path / "round-01.prompt.txt").read_text()
    assert json.loads((tmp_path / "coverage.json").read_text())["unseen_count"] == 22


@pytest.mark.asyncio
async def test_smart_search_enforces_person_time_and_inventory_scope(tmp_path):
    rows = catalog()
    rows[25]["people"] = [{"id": "p1", "name": "Supplied name"}]
    p = Provider()
    a = Analyzer(
        request(
            "search",
            query="restaurant",
            person_ids=["p1"],
            started_at="2026-01-01T00:20:00Z",
        )
    )
    result = await PhotoExplorer(p, a, overview_size=2).explore(
        rows, "UTC", artifact_dir=tmp_path
    )
    assert p.search_calls[0][1]["allowed_ids"] == {"a025"}
    assert result.rounds[1]["offered"] == ["a025"]


@pytest.mark.asyncio
async def test_inspection_uses_larger_previews_for_already_seen_assets(tmp_path):
    p = Provider()
    a = Analyzer(request("inspect", asset_ids=["a000"]))
    result = await PhotoExplorer(p, a, overview_size=2).explore(
        catalog(), "UTC", artifact_dir=tmp_path
    )
    assert [x[1] for x in p.calls] == ["thumbnail", "preview"]
    assert len(a.calls[1][0]) == 2 and len(result.images) == 2


@pytest.mark.asyncio
async def test_unseen_or_external_inspection_is_rejected(tmp_path):
    p = Provider()
    a = Analyzer(request("inspect", asset_ids=["external"]))
    with pytest.raises(ValueError, match="previously offered"):
        await PhotoExplorer(p, a).explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert len(p.calls) == 1


@pytest.mark.asyncio
async def test_round_budget_preserves_uninspected_count(tmp_path):
    p = Provider()
    a = Analyzer(request("sample"))
    result = await PhotoExplorer(p, a, overview_size=4, max_rounds=1).explore(
        catalog(), "UTC", artifact_dir=tmp_path
    )
    assert result.stop_reason == "budget_exhausted"
    assert json.loads((tmp_path / "coverage.json").read_text())["unseen_count"] == 26


@pytest.mark.asyncio
async def test_exact_input_cache_replays_without_another_model_call(tmp_path):
    p = Provider()
    a = Analyzer()
    explorer = PhotoExplorer(p, a, overview_size=4)
    await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert len(a.calls) == 1
    a.identity = "changed-model"
    await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert len(a.calls) == 2


@pytest.mark.asyncio
async def test_completed_round_is_persisted_before_later_search_failure(tmp_path):
    class BrokenSearch(Provider):
        async def search(self, *_args, **_kwargs):
            raise ValueError("Search unavailable")

    settled = []

    async def persist(observations, images, failures):
        settled.append(set(observations))

    with pytest.raises(ValueError, match="Search unavailable"):
        await PhotoExplorer(
            BrokenSearch(), Analyzer(request("search", query="cafe")), overview_size=4
        ).explore(catalog(), "UTC", artifact_dir=tmp_path, on_round=persist)
    assert len(settled) == 1 and len(settled[0]) == 4
    assert len(list(tmp_path.glob("response-*.json"))) == 1


@pytest.mark.asyncio
async def test_invalid_response_does_not_poison_next_attempt(tmp_path):
    analyzer = Analyzer(request("inspect", asset_ids=["invented"]), request())
    explorer = PhotoExplorer(Provider(), analyzer, overview_size=4)
    with pytest.raises(ValueError, match="previously offered"):
        await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert not list(tmp_path.glob("response-*.json"))
    result = await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert result.stop_reason == "model_finished" and len(analyzer.calls) == 2


@pytest.mark.asyncio
async def test_omitted_asset_is_given_another_model_attempt(tmp_path):
    class OmittedFirst(Analyzer):
        async def analyze(self, images, prompt):
            raw = await super().analyze(images, prompt)
            if len(self.calls) == 1:
                raw["images"] = raw["images"][:-1]
            return raw

    analyzer = OmittedFirst()
    explorer = PhotoExplorer(Provider(), analyzer, overview_size=4)
    first = await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert len(first.observations) == 3 and len(first.failures) == 1
    assert not list(tmp_path.glob("response-*.json"))
    second = await explorer.explore(catalog(), "UTC", artifact_dir=tmp_path)
    assert len(second.observations) == 4 and not second.failures
    assert len(analyzer.calls) == 2
