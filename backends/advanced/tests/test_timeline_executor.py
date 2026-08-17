import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from advanced_omi_backend.services.memory.agent.pi_agent import _PiRuntimeConfig
from advanced_omi_backend.services.timeline.codex_executor import (
    CodexTimelineExecutor,
    _parse_usage,
    _workspace_fingerprint,
)
from advanced_omi_backend.services.timeline.context import (
    TimelineContextEvent,
    TimelineContextSummary,
)
from advanced_omi_backend.services.timeline.contracts import (
    AgentAssertion,
    AgentAttribute,
    AgentEpisode,
    TimelineAgentResult,
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
    UnassignedInterval,
)
from advanced_omi_backend.services.timeline.executor import (
    TimelineIncompleteSegmentation,
    build_executor,
    settings_dict,
    validate_agent_result,
)
from advanced_omi_backend.services.timeline.pi_executor import (
    PiTimelineExecutor,
    TimelineWorkspaceError,
    _local_day_instruction,
    _repair_context_json_scaffolding,
    _repair_quoted_object_delimiters,
    _TimelineWorkspaceTools,
)
from advanced_omi_backend.services.timeline.pi_executor import (
    _workspace_fingerprint as _pi_workspace_fingerprint,
)


def _manifest() -> TimelineEvidenceManifest:
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    item = TimelineEvidenceItem(
        evidence_id="observation:one",
        kind="observation",
        started_at=start + timedelta(minutes=2),
        ended_at=start + timedelta(minutes=25),
        role="application_state",
        excerpt="Terminator playback",
    )
    return TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=start,
        ended_at=end,
        evidence_revision="revision",
        windows=[
            TimelineCoverageWindow(
                window_id="window:one",
                started_at=start,
                ended_at=end,
                evidence_ids=[item.evidence_id],
            )
        ],
        evidence=[item],
    )


def _result(evidence_id: str = "observation:one") -> TimelineAgentResult:
    manifest = _manifest()
    return TimelineAgentResult(
        episodes=[
            AgentEpisode(
                kind="media_watched",
                title="Watched Terminator",
                summary="A long movie playback session.",
                started_at=manifest.started_at + timedelta(minutes=2),
                ended_at=manifest.started_at + timedelta(minutes=25),
                salience="routine",
                activity_mode="background",
                confidence=0.9,
                attributes=[AgentAttribute(key="media", value="Terminator")],
                evidence_ids=[evidence_id],
                assertions=[
                    AgentAssertion(
                        claim="Terminator was playing",
                        role="application_state",
                        confidence=0.9,
                        evidence_ids=[evidence_id],
                    )
                ],
            )
        ],
    )


def test_pi_prompt_names_cross_utc_midnight_local_day_bounds():
    manifest = _manifest()
    manifest.local_date = date(2026, 8, 12)
    manifest.timezone = "Asia/Kolkata"
    manifest.started_at = datetime(2026, 8, 11, 18, 30, tzinfo=timezone.utc)
    manifest.ended_at = datetime(2026, 8, 12, 18, 30, tzinfo=timezone.utc)

    instruction = _local_day_instruction(manifest)

    assert "2026-08-12 in Asia/Kolkata" in instruction
    assert "2026-08-11T18:30:00+00:00" in instruction
    assert "previous calendar date" in instruction


def test_glimmer_quoted_object_delimiter_repair_is_lexically_scoped():
    raw = (
        '{"events":[{"summary":"literal },\\"{ text"},'
        '"{"summary":"next"}],"unresolved_evidence_ids":[]}'
    )

    repaired, count = _repair_quoted_object_delimiters(raw)

    assert count == 1
    assert repaired == (
        '{"events":[{"summary":"literal },\\"{ text"},'
        '{"summary":"next"}],"unresolved_evidence_ids":[]}'
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{"events":[{"summary":"one"},"events":[{"summary":"two"}],'
            '"unresolved_evidence_ids":[]}',
            {
                "events": [{"summary": "one"}, {"summary": "two"}],
                "unresolved_evidence_ids": [],
            },
        ),
        (
            '{"events":[{"summary":"one"},"unresolved_evidence_ids":[],'
            '"events":[{"summary":"two"}]}}',
            {"events": [{"summary": "one"}, {"summary": "two"}]},
        ),
        (
            '{"events":[{"summary":"one"},"unresolved_evidence_ids":[]}',
            {"events": [{"summary": "one"}], "unresolved_evidence_ids": []},
        ),
        (
            '{"events":[{"summary":"one"}]',
            {"events": [{"summary": "one"}]},
        ),
    ],
)
def test_glimmer_context_scaffolding_repair_matches_retained_failures(raw, expected):
    repaired, count = _repair_context_json_scaffolding(raw)

    assert count >= 1
    assert json.loads(repaired) == expected


def test_context_scaffolding_repair_does_not_touch_quoted_evidence_text():
    raw = (
        '{"events":[{"summary":"literal },\\"events\\":[{ text"}],'
        '"unresolved_evidence_ids":[]}'
    )

    repaired, count = _repair_context_json_scaffolding(raw)

    assert count == 0
    assert repaired == raw


def test_build_executor_selects_pi_without_codex(monkeypatch):
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.executor.settings_dict",
        lambda: {"executor": "pi", "pi": {"operation": "timeline_segmentation"}},
    )

    executor = build_executor()

    assert isinstance(executor, PiTimelineExecutor)
    assert executor.settings["operation"] == "timeline_segmentation"


def test_timeline_settings_accept_plain_mapping_from_config_loader(monkeypatch):
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.executor.load_config",
        lambda: {"timeline": {"executor": "pi"}},
    )

    assert settings_dict() == {"executor": "pi"}


def test_timeline_workspace_tools_read_json_without_markdown_normalization(tmp_path):
    windows = tmp_path / "windows"
    windows.mkdir()
    (windows / "0000.json").write_text('{"evidence": ["one"]}\n', encoding="utf-8")
    tools = _TimelineWorkspaceTools(tmp_path)

    result = tools.dispatch("read_note", {"path": "windows/0000.json"})

    assert result == '{"evidence": ["one"]}\n'


def test_timeline_workspace_tools_only_write_result_or_work_notes(tmp_path):
    tools = _TimelineWorkspaceTools(tmp_path)

    tools.dispatch(
        "write_note",
        {"path": "timeline-result.json", "content": '{"episodes": []}'},
    )
    tools.dispatch(
        "write_note",
        {"path": "work/summary.txt", "content": "compact notes"},
    )

    assert (tmp_path / "timeline-result.json").is_file()
    assert (tmp_path / "work" / "summary.txt").is_file()
    with pytest.raises(TimelineWorkspaceError, match="write path"):
        tools.dispatch(
            "write_note",
            {"path": "windows/0000.json", "content": "overwrite evidence"},
        )
    with pytest.raises(TimelineWorkspaceError, match="inside the workspace"):
        tools.dispatch("read_note", {"path": "../secret"})


@pytest.mark.parametrize(
    ("failure_kind", "expected_retry_instruction"),
    [
        ("invalid_json", "previous response was invalid json"),
        ("truncated", "previous response hit the output limit"),
    ],
)
@pytest.mark.asyncio
async def test_pi_timeline_executor_passes_compact_context_directly_and_uses_pi_cache_namespace(
    tmp_path, monkeypatch, failure_kind, expected_retry_instruction
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("timeline", encoding="utf-8")
    cache_lookups = []
    calls = []
    artifacts = []

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor._resolve_pi_config",
        lambda operation: _PiRuntimeConfig(
            binary="pi",
            model="muse-glimmer-30B-kquant-17gb.gguf",
            provider="chronicle-llamacpp",
            base_url="http://llama.cpp/v1",
            api_key="no-key",
            thinking="high",
            max_tokens=4096,
            context_window=131072,
            timeout_seconds=900,
            reasoning=True,
            temperature=1.0,
            system_prompt_prefix="Reasoning strength: high",
        ),
    )
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor.load_reusable_result",
        lambda operation, request: cache_lookups.append((operation, request)) or None,
    )
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor.persist_inference_run",
        lambda **kwargs: artifacts.append(kwargs) or ("request", "artifact"),
    )

    async def fake_invoke(root, **kwargs):
        calls.append(kwargs)
        first_call = len(calls) == 1
        return (
            SimpleNamespace(
                truncated=first_call and failure_kind == "truncated",
                fatal_errors=[],
                errors=[],
                summary=(
                    '{"episodes":[}' if first_call else _result().model_dump_json()
                ),
                usage={"input_tokens": 10, "output_tokens": 20},
                rounds=1,
                tool_calls=0,
            ),
            SimpleNamespace(call_count=0),
        )

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor._invoke_pi", fake_invoke
    )

    result = await PiTimelineExecutor({"max_tokens": 24000}).analyze(
        workspace, _manifest(), [], reasoning_effort="low"
    )

    assert cache_lookups[0][0] == "pi_timeline"
    assert cache_lookups[0][1]["model"] == "muse-glimmer-30B-kquant-17gb.gguf"
    assert calls[0]["load_vault_skill"] is False
    assert len(calls) == 2
    assert calls[0]["config"].max_tokens == 24000
    assert calls[0]["config"].thinking == "low"
    assert calls[0]["config"].system_prompt_prefix == "Reasoning strength: low"
    assert calls[0]["schemas"] == ()
    assert calls[0]["max_tool_rounds"] == 1
    assert "observation:one" in calls[0]["prompt"]
    assert "Return only" in calls[0]["system_prompt"]
    assert expected_retry_instruction in calls[1]["prompt"].lower()
    assert artifacts[0]["reusable"] is False
    assert artifacts[-1]["reusable"] is True
    assert result.usage == {
        "input_tokens": 20,
        "output_tokens": 40,
        "context_blocks": 1,
        "context_dense_blocks": 0,
    }


@pytest.mark.asyncio
async def test_dense_context_uses_a_separate_local_agent_before_final_segmentation(
    tmp_path, monkeypatch
):
    manifest = _manifest()
    executor = PiTimelineExecutor({"condense_min_items": 1})
    calls = []

    async def fake_condense(block, *, config, manifest):
        calls.append(block["block_id"])
        item = block["evidence"][0]
        return (
            TimelineContextSummary(
                events=[
                    TimelineContextEvent(
                        started_at=item["started_at"],
                        ended_at=item["ended_at"],
                        summary="Local Glimmer condensed the screen activity.",
                        evidence_ids=item["evidence_ids"],
                        modalities=[item["kind"]],
                    )
                ]
            ),
            {"input_tokens": 50, "output_tokens": 10},
        )

    monkeypatch.setattr(executor, "_condense_context_block", fake_condense)

    stats, usage = await executor._prepare_context_workspace(
        tmp_path, manifest, config=SimpleNamespace()
    )

    payload = json.loads((tmp_path / "context" / "0000.json").read_text())
    assert calls == ["context-0000"]
    assert payload["mode"] == "local_agent_summary"
    assert payload["events"][0]["evidence_ids"] == ["observation:one"]
    assert stats == {
        "block_count": 1,
        "dense_block_count": 1,
        "source_evidence_count": 1,
    }
    assert usage == {"input_tokens": 50, "output_tokens": 10}


@pytest.mark.asyncio
async def test_context_workspace_can_be_rebuilt_for_reasoning_escalation(tmp_path):
    executor = PiTimelineExecutor({})

    first_stats, first_usage = await executor._prepare_context_workspace(
        tmp_path, _manifest(), config=SimpleNamespace()
    )
    second_stats, second_usage = await executor._prepare_context_workspace(
        tmp_path, _manifest(), config=SimpleNamespace()
    )

    assert second_stats == first_stats
    assert second_usage == first_usage
    assert json.loads((tmp_path / "context" / "index.json").read_text())["blocks"]


@pytest.mark.asyncio
async def test_context_condenser_passes_bounded_block_directly_without_tool_loop(
    monkeypatch,
):
    """A condenser must not spend repeated Pi rounds reading one already-bounded blob."""

    manifest = _manifest()
    block = {
        "block_id": "context-0000",
        "started_at": manifest.started_at.isoformat(),
        "ended_at": manifest.ended_at.isoformat(),
        "evidence": [
            {
                "evidence_ids": ["observation:one"],
                "kind": "observation",
                "started_at": manifest.started_at.isoformat(),
                "ended_at": manifest.ended_at.isoformat(),
                "excerpt": "Terminator playback",
            }
        ],
    }
    expected = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                summary="Terminator was playing.",
                evidence_ids=["observation:one"],
                modalities=["observation"],
            )
        ]
    )
    calls = []

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor.load_reusable_result",
        lambda operation, request: None,
    )
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor.persist_inference_run",
        lambda **kwargs: ("request", "artifact"),
    )

    async def fake_invoke(root, **kwargs):
        calls.append(kwargs)
        return (
            SimpleNamespace(
                truncated=False,
                fatal_errors=[],
                errors=[],
                summary=expected.model_dump_json(),
                usage={"input_tokens": 100, "output_tokens": 20},
                rounds=1,
                tool_calls=0,
            ),
            SimpleNamespace(call_count=0),
        )

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor._invoke_pi", fake_invoke
    )
    config = _PiRuntimeConfig(
        binary="pi",
        model="muse-glimmer",
        provider="chronicle-llamacpp",
        base_url="http://llama.cpp/v1",
        api_key="no-key",
        thinking="high",
        max_tokens=12000,
        context_window=131072,
        timeout_seconds=900,
        reasoning=True,
        temperature=1.0,
        system_prompt_prefix="Reasoning strength: high",
    )

    summary, usage = await PiTimelineExecutor({})._condense_context_block(
        block, config=config, manifest=manifest
    )

    assert summary == expected
    assert usage == {"input_tokens": 100, "output_tokens": 20}
    assert calls[0]["schemas"] == ()
    assert calls[0]["max_tool_rounds"] == 1
    assert calls[0]["config"].max_tokens == 12000
    assert calls[0]["config"].thinking == "low"
    assert calls[0]["config"].system_prompt_prefix == "Reasoning strength: low"
    assert "representative IDs" in calls[0]["system_prompt"]
    assert '"observation:one"' in calls[0]["prompt"]


@pytest.mark.asyncio
async def test_context_condenser_retries_invalid_json_and_archives_bad_output(
    monkeypatch,
):
    manifest = _manifest()
    block = {
        "block_id": "context-0000",
        "started_at": manifest.started_at.isoformat(),
        "ended_at": manifest.ended_at.isoformat(),
        "evidence": [
            {
                "evidence_ids": ["observation:one"],
                "kind": "observation",
                "started_at": manifest.started_at.isoformat(),
                "ended_at": manifest.ended_at.isoformat(),
                "excerpt": "Terminator playback",
            }
        ],
    }
    expected = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                summary="Terminator was playing.",
                evidence_ids=["observation:one"],
                modalities=["observation"],
            )
        ]
    )
    calls = []
    artifacts = []
    responses = ['{"events":[}', expected.model_dump_json()]

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor.load_reusable_result",
        lambda operation, request: None,
    )
    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor.persist_inference_run",
        lambda **kwargs: artifacts.append(kwargs) or ("request", "artifact"),
    )

    async def fake_invoke(root, **kwargs):
        calls.append(kwargs)
        return (
            SimpleNamespace(
                truncated=False,
                fatal_errors=[],
                errors=[],
                summary=responses[len(calls) - 1],
                usage={"input_tokens": 10, "output_tokens": 20},
                rounds=1,
                tool_calls=0,
            ),
            SimpleNamespace(call_count=0),
        )

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.pi_executor._invoke_pi", fake_invoke
    )
    config = _PiRuntimeConfig(
        binary="pi",
        model="muse-glimmer",
        provider="chronicle-llamacpp",
        base_url="http://llama.cpp/v1",
        api_key="no-key",
        thinking="high",
        max_tokens=12000,
        context_window=131072,
        timeout_seconds=900,
        reasoning=True,
        temperature=1.0,
        system_prompt_prefix="Reasoning strength: high",
    )

    summary, usage = await PiTimelineExecutor({})._condense_context_block(
        block, config=config, manifest=manifest
    )

    assert summary == expected
    assert usage == {"input_tokens": 20, "output_tokens": 40}
    assert len(calls) == 2
    assert "previous response was invalid json" in calls[1]["prompt"].lower()
    assert artifacts[0]["reusable"] is False
    assert artifacts[0]["stdout"] == '{"events":[}'
    assert artifacts[-1]["reusable"] is True


def test_workspace_fingerprint_tracks_inputs_but_ignores_generated_outputs(tmp_path):
    (tmp_path / "evidence.json").write_text("first", encoding="utf-8")
    (tmp_path / "timeline-result.json").write_text("generated", encoding="utf-8")

    first = _workspace_fingerprint(tmp_path)
    (tmp_path / "evidence.json").write_text("second", encoding="utf-8")
    second = _workspace_fingerprint(tmp_path)

    assert [entry["path"] for entry in first] == ["evidence.json"]
    assert first != second


def test_pi_workspace_fingerprint_ignores_generated_context(tmp_path):
    (tmp_path / "evidence.json").write_text("source", encoding="utf-8")
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "index.json").write_text("generated", encoding="utf-8")

    assert [entry["path"] for entry in _pi_workspace_fingerprint(tmp_path)] == [
        "evidence.json"
    ]


@pytest.mark.asyncio
async def test_cached_timeline_result_bypasses_quota_and_provider(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cached_result = _result().model_dump(mode="json")

    monkeypatch.setattr(
        "advanced_omi_backend.services.timeline.codex_executor.load_reusable_result",
        lambda operation, request: cached_result,
    )
    executor = CodexTimelineExecutor({})
    monkeypatch.setattr(
        executor,
        "_check_quota",
        lambda: pytest.fail("cache hit must happen before quota or provider checks"),
    )

    result = await executor.analyze(workspace, _manifest(), [])

    assert result.episodes[0].title == "Watched Terminator"
    assert result.usage["cache_hits"] == 1


def test_valid_overlapping_episode_result_is_accepted():
    validate_agent_result(_result(), _manifest())


def test_sub_millisecond_episode_remains_positive_after_bson_round_trip():
    manifest = _manifest()
    point = manifest.evidence[0]
    point.started_at = point.started_at.replace(microsecond=792974)
    point.ended_at = None
    result = _result()
    result.episodes[0].started_at = point.started_at
    result.episodes[0].ended_at = point.started_at + timedelta(microseconds=1)

    validate_agent_result(result, manifest)

    episode = result.episodes[0]
    assert episode.started_at.microsecond == 792000
    assert episode.ended_at.microsecond == 793000
    assert episode.ended_at > episode.started_at


def test_episode_cannot_bridge_a_large_interval_with_no_evidence():
    manifest = _manifest()
    manifest.ended_at = manifest.started_at + timedelta(hours=12)
    first = manifest.evidence[0]
    first.ended_at = first.started_at + timedelta(minutes=2)
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        started_at=manifest.started_at + timedelta(hours=11),
        ended_at=manifest.started_at + timedelta(hours=11, minutes=1),
        role="application_state",
        excerpt="Unrelated activity much later",
    )
    manifest.evidence.append(later)
    result = _result()
    episode = result.episodes[0]
    episode.started_at = first.started_at
    episode.ended_at = later.ended_at
    episode.evidence_ids.append(later.evidence_id)

    with pytest.raises(TimelineIncompleteSegmentation, match="uncaptured internal gap"):
        validate_agent_result(result, manifest)


def test_final_attempt_can_drop_one_gap_bridging_episode_and_keep_valid_episodes():
    manifest = _manifest()
    manifest.ended_at = manifest.started_at + timedelta(hours=12)
    first = manifest.evidence[0]
    first.ended_at = first.started_at + timedelta(minutes=2)
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        started_at=manifest.started_at + timedelta(hours=11),
        ended_at=manifest.started_at + timedelta(hours=11, minutes=1),
        role="application_state",
        excerpt="Unrelated activity much later",
    )
    manifest.evidence.append(later)
    result = _result()
    bridged = result.episodes[0]
    bridged.started_at = first.started_at
    bridged.ended_at = later.ended_at
    bridged.evidence_ids.append(later.evidence_id)
    valid = _result(later.evidence_id).episodes[0]
    valid.started_at = later.started_at
    valid.ended_at = later.ended_at
    valid.title = "Later valid activity"
    result.episodes.append(valid)

    validate_agent_result(
        result,
        manifest,
        salvage_gap_bridging_episodes=True,
    )

    assert [episode.title for episode in result.episodes] == ["Later valid activity"]
    assert result.unassigned_intervals


def test_uncited_intermediate_evidence_can_support_one_continuous_episode():
    manifest = _manifest()
    start = manifest.started_at + timedelta(minutes=2)
    manifest.evidence = [
        TimelineEvidenceItem(
            evidence_id=f"observation:{index}",
            kind="observation",
            started_at=start + timedelta(minutes=4 * index),
            ended_at=start + timedelta(minutes=4 * index + 5),
            role="application_state",
            excerpt="One continuous application session",
        )
        for index in range(4)
    ]
    result = _result("observation:0")
    result.episodes[0].started_at = manifest.evidence[0].started_at
    result.episodes[0].ended_at = manifest.evidence[-1].ended_at
    result.episodes[0].evidence_ids.append("observation:3")

    validate_agent_result(result, manifest)


def test_a_day_whose_only_episode_is_malformed_fails():
    """Dropping bad episodes must not quietly turn a broken run into an empty day."""

    with pytest.raises(TimelineIncompleteSegmentation, match="unknown evidence"):
        validate_agent_result(_result("observation:invented"), _manifest())


def test_unknown_citation_is_removed_when_valid_evidence_remains():
    """One hallucinated citation must not discard an otherwise grounded episode."""

    result = _result()
    result.episodes[0].evidence_ids.append("observation:invented")
    result.episodes[0].assertions[0].evidence_ids.append("observation:invented")

    validate_agent_result(result, _manifest())

    episode = result.episodes[0]
    assert episode.evidence_ids == ["observation:one"]
    assert episode.assertions[0].evidence_ids == ["observation:one"]


def test_unique_evidence_suffix_is_restored_when_model_drops_kind_prefix():
    result = _result("one")

    validate_agent_result(result, _manifest())

    episode = result.episodes[0]
    assert episode.evidence_ids == ["observation:one"]
    assert episode.assertions[0].evidence_ids == ["observation:one"]


def test_ambiguous_evidence_suffix_is_not_guessed():
    manifest = _manifest()
    original = manifest.evidence[0]
    manifest.evidence.append(
        TimelineEvidenceItem(
            evidence_id="transcript:one",
            kind="transcript",
            started_at=original.started_at,
            ended_at=original.ended_at,
            role="uncertain",
            excerpt="Same suffix from a different evidence kind",
        )
    )

    with pytest.raises(TimelineIncompleteSegmentation, match="unknown evidence"):
        validate_agent_result(_result("one"), manifest)


def test_saying_nothing_about_a_day_with_evidence_is_a_failure_not_an_answer():
    """Regression: this was tolerated, and every empty run silently blanked a day.

    Materializing the gap as "unassigned" made a model failure look like a successful
    analysis, so the day published zero episodes and superseded a good generation.
    """

    result = TimelineAgentResult(episodes=[], unassigned_intervals=[])

    with pytest.raises(TimelineIncompleteSegmentation):
        validate_agent_result(result, _manifest())


def test_evidence_the_agent_partially_explained_is_still_materialized():
    """The materialize behaviour is right once the agent has said *something*."""

    manifest = _manifest()
    result = _result()
    # Cited evidence spans 2-25min; the episode only covers 2-10min.
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=10)

    validate_agent_result(result, manifest)

    assert result.unassigned_intervals
    assert result.unassigned_intervals[-1].ended_at == manifest.started_at + timedelta(
        minutes=25
    )


def test_unavailable_representative_image_is_dropped_without_losing_episode():
    result = _result()
    result.episodes[0].representative_evidence_id = "observation:one"
    validate_agent_result(result, _manifest())
    assert result.episodes[0].representative_evidence_id is None


def test_non_overlapping_citation_is_pruned_when_episode_remains_grounded():
    manifest = _manifest()
    outside = TimelineEvidenceItem(
        evidence_id="observation:outside",
        kind="observation",
        started_at=manifest.started_at + timedelta(minutes=27),
        ended_at=manifest.started_at + timedelta(minutes=28),
        role="application_state",
        excerpt="Later application state",
    )
    manifest.evidence.append(outside)
    result = _result()
    result.episodes[0].evidence_ids.append(outside.evidence_id)
    result.unassigned_intervals.append(
        UnassignedInterval(
            started_at=outside.started_at,
            ended_at=outside.ended_at,
            reason="Separate later evidence",
        )
    )
    validate_agent_result(result, manifest)
    assert result.episodes[0].evidence_ids == ["observation:one"]


def test_episode_without_overlapping_citation_is_rejected():
    manifest = _manifest()
    result = _result()
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=26)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=27)
    with pytest.raises(
        TimelineIncompleteSegmentation, match="no temporally overlapping evidence"
    ):
        validate_agent_result(result, manifest)


def test_exact_boundary_observations_ground_an_episode():
    """Zero-duration screen observations may define both closed boundaries."""

    manifest = _manifest()
    start = manifest.started_at + timedelta(minutes=10)
    end = start + timedelta(seconds=9)
    manifest.evidence = [
        TimelineEvidenceItem(
            evidence_id="observation:start",
            kind="observation",
            started_at=start,
            ended_at=start,
            role="application_state",
            excerpt="Search view opened",
        ),
        TimelineEvidenceItem(
            evidence_id="observation:end",
            kind="observation",
            started_at=end,
            ended_at=end + timedelta(minutes=5),
            role="application_state",
            excerpt="Search view closed",
        ),
    ]
    manifest.windows[0].evidence_ids = [item.evidence_id for item in manifest.evidence]
    result = _result()
    episode = result.episodes[0]
    episode.started_at = start
    episode.ended_at = end
    episode.evidence_ids = [item.evidence_id for item in manifest.evidence]
    episode.assertions[0].evidence_ids = list(episode.evidence_ids)

    validate_agent_result(result, manifest)

    assert result.episodes[0].evidence_ids == [
        "observation:start",
        "observation:end",
    ]


def test_a_malformed_episode_is_dropped_without_losing_the_good_ones():
    """The whole day used to be discarded over one bad episode.

    Under --output-schema the agent's planning narration is schema-valid, so a stray
    entry can appear beside real episodes; rejecting the run threw all of them away.
    """

    manifest = _manifest()
    result = _result()
    good = result.episodes[0]
    narration = good.model_copy(
        update={
            "kind": "task",
            "title": "Inspect Chronicle day inputs",
            # Cites real evidence but sits outside it, as the leaked planning entries did.
            "started_at": manifest.started_at + timedelta(minutes=26),
            "ended_at": manifest.started_at + timedelta(minutes=27),
        }
    )
    result.episodes = [narration, good]

    validate_agent_result(result, manifest)

    assert [episode.title for episode in result.episodes] == ["Watched Terminator"]


def test_dropping_a_malformed_episode_remaps_parent_indices():
    manifest = _manifest()
    result = _result()
    good = result.episodes[0]
    narration = good.model_copy(
        update={
            "title": "planning noise",
            "started_at": manifest.started_at + timedelta(minutes=26),
            "ended_at": manifest.started_at + timedelta(minutes=27),
        }
    )
    child = good.model_copy(update={"title": "child", "parent_episode_index": 1})
    result.episodes = [narration, good, child]

    validate_agent_result(result, manifest)

    titles = [episode.title for episode in result.episodes]
    assert titles == ["Watched Terminator", "child"]
    # `good` moved from index 1 to 0, so the child's parent pointer must follow it.
    assert result.episodes[1].parent_episode_index == 0


def test_unsupported_episode_end_is_deterministically_bounded_to_cited_evidence():
    manifest = _manifest()
    manifest.evidence[0].ended_at = manifest.started_at + timedelta(minutes=5)
    result = _result()

    validate_agent_result(result, manifest)

    assert result.episodes[0].ended_at == manifest.evidence[0].ended_at


def test_unsupported_episode_start_is_deterministically_bounded_to_cited_evidence():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    validate_agent_result(result, manifest)

    assert result.episodes[0].started_at == manifest.evidence[0].started_at


def test_point_only_citation_cannot_be_repaired_into_a_positive_episode():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    manifest.evidence[0].ended_at = manifest.evidence[0].started_at
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    with pytest.raises(TimelineIncompleteSegmentation, match="positive cited interval"):
        validate_agent_result(result, manifest)


def test_naive_agent_timestamps_are_interpreted_as_utc():
    manifest = _manifest()
    result = _result()
    result.episodes[0].started_at = result.episodes[0].started_at.replace(tzinfo=None)
    result.episodes[0].ended_at = result.episodes[0].ended_at.replace(tzinfo=None)

    validate_agent_result(result, manifest)

    assert result.episodes[0].started_at.tzinfo == timezone.utc
    assert result.episodes[0].ended_at.tzinfo == timezone.utc


def test_unexplained_evidence_is_materialized_as_an_unassigned_interval():
    manifest = _manifest()
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        started_at=manifest.started_at + timedelta(minutes=27),
        ended_at=manifest.started_at + timedelta(minutes=29),
        role="application_state",
    )
    manifest.evidence.append(later)

    result = _result()

    validate_agent_result(result, manifest)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].started_at == later.started_at
    assert result.unassigned_intervals[0].ended_at == later.ended_at
    assert result.unassigned_intervals[0].reason == (
        "Evidence was not assigned by semantic analysis"
    )


def test_unassigned_intervals_outside_manifest_are_discarded():
    manifest = _manifest()
    result = _result()
    result.unassigned_intervals.append(
        UnassignedInterval(
            started_at=manifest.started_at - timedelta(minutes=1),
            ended_at=manifest.started_at,
            reason="Invalid outer interval",
        )
    )

    validate_agent_result(result, manifest)

    assert result.unassigned_intervals == []


def test_pinned_intervals_account_for_their_evidence():
    """A day fully covered by a confirmed episode needs no drafted episode."""

    manifest = _manifest()
    result = TimelineAgentResult(episodes=[])
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []
    assert result.unassigned_intervals == []


def test_evidence_outside_a_pinned_interval_is_still_unaccounted():
    manifest = _manifest()
    result = TimelineAgentResult(episodes=[])
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=10),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].ended_at == manifest.started_at + timedelta(
        minutes=25
    )


def test_drafted_episode_repeating_a_pinned_interval_is_dropped():
    manifest = _manifest()
    result = _result()
    pinned = [(result.episodes[0].started_at, result.episodes[0].ended_at)]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []


def test_a_draft_inside_a_pinned_interval_is_dropped_even_after_clamping():
    """Bound clamping can pull a draft into a confirmed interval; it is still a dupe."""

    manifest = _manifest()
    result = _result()
    # Cited evidence ends at minute 25, so this episode clamps back to 24-25 — entirely
    # inside the pinned interval, though it did not start that way.
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=24)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=29)
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []


def test_dropping_a_pinned_duplicate_remaps_parent_indices():
    manifest = _manifest()
    result = _result()
    duplicate = result.episodes[0].model_copy(
        update={
            "started_at": manifest.started_at + timedelta(minutes=2),
            "ended_at": manifest.started_at + timedelta(minutes=10),
        }
    )
    child = result.episodes[0].model_copy(
        update={
            "title": "Later event",
            "started_at": manifest.started_at + timedelta(minutes=12),
            "ended_at": manifest.started_at + timedelta(minutes=25),
            "parent_episode_index": 0,
        }
    )
    # Index 0 is the pinned duplicate; the survivor's parent index must follow it.
    result.episodes = [duplicate, child]
    pinned = [(duplicate.started_at, duplicate.ended_at)]

    validate_agent_result(result, manifest, pinned)

    assert [episode.title for episode in result.episodes] == ["Later event"]
    assert result.episodes[0].parent_episode_index is None


def test_codex_usage_is_summed_across_turns():
    """An agentic run emits one turn.completed per turn; cost is their sum."""

    stdout = b"""{"type":"thread.started","thread_id":"t"}
{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10,"reasoning_output_tokens":2}}
{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}
{"type":"turn.completed","usage":{"input_tokens":250,"cached_input_tokens":200,"output_tokens":30,"reasoning_output_tokens":5}}
"""

    assert _parse_usage(stdout) == {
        "input_tokens": 350,
        "cached_input_tokens": 240,
        "output_tokens": 40,
        "reasoning_output_tokens": 7,
        "turns": 2,
    }


def test_missing_or_malformed_usage_events_are_not_an_error():
    """Usage is accounting; losing it must never fail a completed analysis."""

    assert _parse_usage(b"") == {}
    assert _parse_usage(b"not json\n{broken\n") == {}
    assert _parse_usage(b'{"type":"turn.completed"}\n') == {"turns": 1}


def test_a_day_fully_covered_by_confirmed_episodes_may_add_nothing():
    """Pinned coverage is a real account of the day, so silence is legitimate."""

    manifest = _manifest()
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(TimelineAgentResult(episodes=[]), manifest, pinned)


def test_partially_pinned_coverage_still_rejects_unexplained_evidence():
    manifest = _manifest()
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=6),
        )
    ]
    result = TimelineAgentResult(episodes=[])

    validate_agent_result(result, manifest, pinned)

    # Not silently dropped: the unexplained tail is materialized as unknown time.
    assert result.unassigned_intervals


def test_unassigned_cause_distinguishes_blackout_from_unexplained_capture():
    """The agent's prose is not trusted for *why* an interval is unassigned.

    A run was observed labelling a multi-hour recording blackout "no evidence supports
    a coherent episode", which reads as a segmentation failure rather than a gap in
    capture. The cause is derived from the manifest instead.
    """

    manifest = _manifest()
    blackout = TimelineEvidenceItem(
        evidence_id="capture_gap:one",
        kind="capture_gap",
        started_at=manifest.started_at + timedelta(minutes=26),
        ended_at=manifest.ended_at,
        role="application_state",
        excerpt="no capture",
    )
    manifest.evidence.append(blackout)
    manifest.windows[0].evidence_ids.append(blackout.evidence_id)

    result = _result()
    # Leave the tail of the observation to the unassigned interval below.
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=15)
    result.unassigned_intervals = [
        UnassignedInterval(
            started_at=manifest.started_at + timedelta(minutes=15),
            ended_at=manifest.started_at + timedelta(minutes=25),
            reason="No evidence supports a coherent episode in this gap.",
        ),
        UnassignedInterval(
            started_at=manifest.started_at + timedelta(minutes=26),
            ended_at=manifest.ended_at,
            reason="No evidence supports a coherent episode in this gap.",
        ),
    ]

    validate_agent_result(result, manifest)

    causes = {
        interval.started_at: interval.cause for interval in result.unassigned_intervals
    }
    # Overlaps the observation: captured, just not explained.
    assert causes[manifest.started_at + timedelta(minutes=15)] == "unexplained"
    # Overlaps only a capture_gap: nothing was recorded to explain.
    assert causes[manifest.started_at + timedelta(minutes=26)] == "no_capture"
