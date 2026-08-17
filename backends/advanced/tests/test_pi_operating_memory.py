import json
from types import SimpleNamespace

import pytest

from advanced_omi_backend.routers.modules import memory_routes
from advanced_omi_backend.services.memory.agent import operating_memory_optimizer
from advanced_omi_backend.services.memory.agent.operating_memory import (
    OperatingMemoryStore,
    OperatingMemoryTools,
    VaultToolError,
)


def test_operating_memory_versions_agents_and_quarantines_script(tmp_path):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")

    result = store.propose_agents(
        "# Learned\n\nSearch aliases before creating a person.",
        rationale="Two runs created case variants.",
        evidence_ids=["trace-a", "trace-b"],
    )
    candidate = store.write_skill_candidate(
        slug="compact-search",
        skill_markdown="# Compact Search\n\nUse grep before broad reads.",
        rationale="Broad reads dominated input tokens.",
        evidence_ids=["trace-b"],
        script_name="compact.py",
        script="print('candidate only')",
    )

    assert "shadow AGENTS.md candidate" in result
    assert store.read_agents() == ""
    assert "inert skill candidate" in candidate
    agents_manifest = json.loads(
        next(
            path
            for path in (store.root / "candidates").glob("*/manifest.json")
            if json.loads(path.read_text()).get("component") == "agents"
        ).read_text()
    )
    assert agents_manifest["status"] == "shadow_candidate"
    manifest = json.loads(
        next(
            path
            for path in (store.root / "candidates").glob("*/manifest.json")
            if json.loads(path.read_text()).get("format")
            == "chronicle-pi-skill-candidate-v1"
        ).read_text()
    )
    assert manifest["status"] == "inert_candidate"
    assert not (store.root / "skills").exists()


def test_agents_candidate_requires_review_before_promotion(tmp_path):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    store.propose_agents(
        "# Learned\n\nSearch exact targets before broad listings.",
        rationale="Repeated broad searches.",
        evidence_ids=["trace-a"],
    )
    manifest_path = next((store.root / "candidates").glob("*/manifest.json"))
    candidate_id = json.loads(manifest_path.read_text())["candidate_id"]

    with pytest.raises(VaultToolError, match="approved"):
        store.promote_agents_candidate(candidate_id)

    store.review_agents_candidate(
        candidate_id,
        decision="approve",
        rationale="Replay improved efficiency without correctness regression.",
        evidence_ids=["evaluation-a", "holdout-a"],
    )
    result = store.promote_agents_candidate(candidate_id)

    assert "Promoted" in result
    assert "Search exact targets" in store.read_agents()
    assert json.loads(manifest_path.read_text())["status"] == "active"
    revision_path = next((store.root / "history").glob("*.json"))
    revision = json.loads(revision_path.read_text())
    assert revision["evidence_ids"] == ["evaluation-a", "holdout-a"]

    store.rollback_agents_revision(revision_path.name)

    rolled_back = json.loads(manifest_path.read_text())
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["rollback_source_revision_id"] == revision_path.name
    assert store.read_agents() == ""


def test_promoting_new_candidate_supersedes_prior_active_manifest(tmp_path):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")

    def propose_and_promote(content, suffix):
        store.propose_agents(
            content,
            rationale=f"Candidate {suffix}.",
            evidence_ids=[f"trace-{suffix}"],
        )
        candidate_id = next(
            candidate["candidate_id"]
            for candidate in store.list_candidates()
            if candidate["status"] == "shadow_candidate"
        )
        store.review_agents_candidate(
            candidate_id,
            decision="approve",
            rationale=f"Candidate {suffix} passed both splits.",
            evidence_ids=[f"dev-{suffix}", f"holdout-{suffix}"],
        )
        store.promote_agents_candidate(candidate_id)
        return candidate_id

    first_id = propose_and_promote("# First\n", "one")
    second_id = propose_and_promote("# Second\n", "two")
    manifests = {
        candidate["candidate_id"]: candidate for candidate in store.list_candidates()
    }

    assert manifests[first_id]["status"] == "superseded"
    assert manifests[first_id]["superseded_by"] == second_id
    assert manifests[second_id]["status"] == "active"


def test_operating_memory_lists_bounded_candidate_metadata_and_detail(tmp_path):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    store.propose_agents(
        "# Learned\n\nSearch exact targets before broad listings.",
        rationale="Repeated broad searches.",
        evidence_ids=["trace-a"],
    )
    store.write_skill_candidate(
        slug="compact-search",
        skill_markdown="# Compact Search\n\nSearch before reading.",
        rationale="Repeated broad reads.",
        evidence_ids=["trace-b"],
        script_name="search.js",
        script="export const search = () => true;",
    )

    candidates = store.list_candidates()

    assert {candidate["component"] for candidate in candidates} == {"agents", "skill"}
    assert len(store.list_candidates(limit=1)) == 1
    assert all("content" not in candidate for candidate in candidates)
    agents = next(
        candidate for candidate in candidates if candidate["component"] == "agents"
    )
    detail = store.read_candidate(agents["candidate_id"])
    assert detail["content_name"] == "AGENTS.md"
    assert "Search exact targets" in detail["content"]
    skill = next(
        candidate for candidate in candidates if candidate["component"] == "skill"
    )
    skill_detail = store.read_candidate(skill["candidate_id"])
    assert skill_detail["script_name"] == "search.js"
    assert "export const search" in skill_detail["script"]


def test_agents_candidate_cannot_overwrite_newer_active_guidance(tmp_path):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    store.propose_agents(
        "# Candidate\n",
        rationale="Trace evidence.",
        evidence_ids=["trace-a"],
    )
    candidate_id = store.list_candidates()[0]["candidate_id"]
    with pytest.raises(VaultToolError, match="development and holdout"):
        store.review_agents_candidate(
            candidate_id,
            decision="approve",
            rationale="Only development replay passed.",
            evidence_ids=["evaluation-a"],
        )
    store.review_agents_candidate(
        candidate_id,
        decision="approve",
        rationale="Development and holdout replays passed.",
        evidence_ids=["evaluation-a", "holdout-a"],
    )
    store.replace_agents(
        "# Newer active guidance\n",
        rationale="A different reviewed proposal won.",
        evidence_ids=["evaluation-b"],
    )

    with pytest.raises(VaultToolError, match="stale"):
        store.promote_agents_candidate(candidate_id)


def test_operating_memory_rollback_restores_prior_agents_and_records_revision(tmp_path):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    store.replace_agents(
        "# First\n",
        rationale="First evaluated guidance.",
        evidence_ids=["evaluation-a"],
    )
    store.replace_agents(
        "# Second\n",
        rationale="Second evaluated guidance.",
        evidence_ids=["evaluation-b"],
    )
    second_revision = store.list_revisions()[0]["revision_id"]

    result = store.rollback_agents_revision(second_revision)

    assert "Rolled back" in result
    assert store.read_agents() == "# First\n"
    revisions = store.list_revisions()
    assert len(revisions) == 3
    assert len(store.list_revisions(limit=2)) == 2
    assert revisions[0]["evidence_ids"] == [f"rollback:{second_revision}"]


def test_optimizer_tools_allow_only_one_component_change(tmp_path):
    tools = OperatingMemoryTools(
        OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    )
    tools.dispatch(
        "propose_agents_memory",
        {
            "content": "# Guidance\n",
            "rationale": "Repeated evidence.",
            "evidence_ids": ["trace-a"],
        },
    )

    with pytest.raises(VaultToolError, match="Only one"):
        tools.dispatch(
            "write_skill_candidate",
            {
                "slug": "second-change",
                "skill_markdown": "# Second\n",
                "rationale": "Same run.",
                "evidence_ids": ["trace-a"],
            },
        )


def test_optimizer_trace_summary_uses_explicit_tool_events_without_content():
    stdout = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "turn_start"},
            {
                "type": "message_update",
                "message": {
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "read_note",
                            "arguments": {"path": "People/Duplicated.md"},
                        }
                    ]
                },
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "read-1",
                "toolName": "read_note",
                "args": {"path": "People/Alice.md", "offset": 0},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "read-1",
                "toolName": "read_note",
                "isError": False,
                "result": {
                    "content": [{"type": "text", "text": "private note text"}],
                    "details": {},
                },
            },
            {"type": "turn_start"},
            {
                "type": "tool_execution_start",
                "toolCallId": "write-1",
                "toolName": "edit_section",
                "args": {
                    "path": "People/Alice.md",
                    "target": "About",
                    "text": "private mutation text",
                },
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "write-1",
                "toolName": "edit_section",
                "isError": False,
                "result": {
                    "content": [{"type": "text", "text": "Error: failed"}],
                    "details": {},
                },
            },
        ]
    )

    summary = operating_memory_optimizer._trace_summary(
        {
            "artifact_hash": "trace-a",
            "request": {"operation": "memory_write", "record": "conversation"},
            "result": {
                "rounds": 3,
                "tool_calls": 2,
                "summary": "private final response about Alice",
            },
            "stdout": stdout,
        }
    )

    assert [
        {key: value for key, value in call.items() if key != "arguments_hash"}
        for call in summary["tool_outline"]
    ] == [
        {
            "turn": 1,
            "tool": "read_note",
            "arguments": {"path": "People/Alice.md", "offset": "0"},
            "is_error": False,
            "output_chars": 17,
        },
        {
            "turn": 2,
            "tool": "edit_section",
            "arguments": {"path": "People/Alice.md"},
            "is_error": True,
            "output_chars": 13,
        },
    ]
    assert all(len(call["arguments_hash"]) == 12 for call in summary["tool_outline"])
    assert "private note text" not in json.dumps(summary)
    assert "private mutation text" not in json.dumps(summary)
    assert "private final response" not in json.dumps(summary)
    assert summary["summary_chars"] == len("private final response about Alice")
    assert len(summary["summary_sha256"]) == 64
    assert summary["efficiency_signals"] == {
        "explicit_tool_events": 2,
        "read_calls": 1,
        "search_calls": 0,
        "mutation_calls": 1,
        "tool_error_count": 1,
        "tool_output_chars": 30,
        "read_output_chars": 17,
        "unique_call_count": 2,
        "repeated_call_count": 0,
        "max_consecutive_identical_calls": 1,
        "max_repeated_cycle_repetitions": 1,
        "repeated_cycle_length": 0,
        "max_calls_in_single_turn": 1,
        "turns_with_parallel_fanout": 0,
        "reported_tool_calls": 2,
        "unoutlined_tool_calls": 0,
        "turns_before_first_mutation": 1,
        "turns_after_last_mutation": 1,
    }


def test_optimizer_trace_summary_preserves_content_free_iteration_feedback():
    summary = operating_memory_optimizer._trace_summary(
        {
            "artifact_hash": "candidate-trace",
            "request": {"operation": "memory_write"},
            "result": {},
            "stdout": "",
            "optimization_feedback": {
                "kind": "rejected_candidate_regression",
                "regressions": ["new_fallback", "agent_completed"],
                "baseline_write_outcome": {"fallback_written": False},
            },
        }
    )

    assert summary["optimization_feedback"] == {
        "kind": "rejected_candidate_regression",
        "regressions": ["new_fallback", "agent_completed"],
        "baseline_write_outcome": {"fallback_written": False},
    }


def test_optimizer_contract_explicitly_forbids_fixed_budget_forms():
    prompt = operating_memory_optimizer._SYSTEM_PROMPT
    normalized = " ".join(prompt.split())

    assert "Never put numeric limits" in prompt
    assert "at most one search" in prompt
    assert "Pi chooses the files" in prompt
    assert "intervening vault mutation" in prompt
    assert "Do not require glob, grep, or read" in prompt
    assert "incremental guidance" in prompt
    assert "Call the proposal tool promptly" in prompt
    assert "Keep trace statistics" in prompt
    assert "one scoped search answers it" in normalized
    assert "Your first response must call" in prompt
    assert "your next response must either record" in normalized


def test_optimizer_trace_summary_counts_repeated_identical_calls():
    call = {
        "turn": 1,
        "tool": "grep",
        "arguments_hash": "same-call",
        "arguments": {"pattern": "Alice"},
        "is_error": False,
        "output_chars": 20,
    }

    signals = operating_memory_optimizer._trace_signals(
        [call, dict(call)], total_rounds=1
    )

    assert signals["repeated_call_count"] == 1
    assert signals["unique_call_count"] == 1
    assert signals["max_consecutive_identical_calls"] == 2
    assert signals["max_repeated_cycle_repetitions"] == 2
    assert signals["repeated_cycle_length"] == 1
    assert signals["max_calls_in_single_turn"] == 2
    assert signals["turns_with_parallel_fanout"] == 0
    assert signals["mutation_calls"] == 0
    assert signals["turns_before_first_mutation"] is None
    assert signals["turns_after_last_mutation"] is None


def test_optimizer_trace_summary_detects_alternating_call_cycle():
    read = {
        "turn": 1,
        "tool": "read_note",
        "arguments_hash": "same-read",
        "arguments": {"path": "People/John.md"},
        "is_error": False,
        "output_chars": 20,
    }
    search = {
        "turn": 1,
        "tool": "grep",
        "arguments_hash": "same-search",
        "arguments": {"pattern": "museum"},
        "is_error": False,
        "output_chars": 20,
    }

    signals = operating_memory_optimizer._trace_signals(
        [read, search, read, search, read, search], total_rounds=3
    )

    assert signals["max_consecutive_identical_calls"] == 1
    assert signals["max_repeated_cycle_repetitions"] == 3
    assert signals["repeated_cycle_length"] == 2


def test_optimizer_trace_summary_detects_repeated_batch_cycle():
    batch = [
        {
            "turn": 1,
            "tool": "grep",
            "arguments_hash": f"batch-call-{index}",
            "arguments": {"pattern": f"topic-{index}"},
            "is_error": False,
            "output_chars": 20,
        }
        for index in range(11)
    ]

    signals = operating_memory_optimizer._trace_signals(
        [*batch, *batch, *batch], total_rounds=3
    )

    assert signals["max_repeated_cycle_repetitions"] == 3
    assert signals["repeated_cycle_length"] == 11


def test_optimizer_outline_keeps_start_and_terminal_tool_events():
    events = []
    for index in range(90):
        events.extend(
            [
                {"type": "turn_start"},
                {
                    "type": "tool_execution_start",
                    "toolCallId": f"call-{index}",
                    "toolName": "grep",
                    "args": {"pattern": f"pattern-{index}"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": f"call-{index}",
                    "toolName": "grep",
                    "isError": False,
                    "result": {"content": [{"type": "text", "text": "none"}]},
                },
            ]
        )

    outline = operating_memory_optimizer._event_outline(
        "\n".join(json.dumps(event) for event in events)
    )

    assert len(outline) == 80
    assert outline[0]["arguments"]["pattern"] == "pattern-0"
    assert outline[39]["arguments"]["pattern"] == "pattern-39"
    assert outline[40]["arguments"]["pattern"] == "pattern-50"
    assert outline[-1]["arguments"]["pattern"] == "pattern-89"


def test_optimizer_trace_summary_bounds_outline_but_keeps_full_outline_signals():
    events = []
    for index in range(90):
        events.extend(
            [
                {"type": "turn_start"},
                {
                    "type": "tool_execution_start",
                    "toolCallId": f"call-{index}",
                    "toolName": "grep",
                    "args": {"pattern": f"pattern-{index}"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": f"call-{index}",
                    "toolName": "grep",
                    "isError": False,
                    "result": {"content": [{"type": "text", "text": "none"}]},
                },
            ]
        )

    summary = operating_memory_optimizer._trace_summary(
        {
            "artifact_hash": "trace-a",
            "request": {"operation": "memory_write"},
            "result": {"rounds": 90, "tool_calls": 90},
            "stdout": "\n".join(json.dumps(event) for event in events),
        }
    )

    assert len(summary["tool_outline"]) == 20
    assert [call["arguments"]["pattern"] for call in summary["tool_outline"][:8]] == [
        f"pattern-{index}" for index in range(8)
    ]
    assert [call["arguments"]["pattern"] for call in summary["tool_outline"][-12:]] == [
        f"pattern-{index}" for index in range(78, 90)
    ]
    assert summary["tool_outline_retained_events"] == 20
    assert summary["tool_outline_omitted_events"] == 60
    assert summary["efficiency_signals"]["explicit_tool_events"] == 80
    assert summary["efficiency_signals"]["reported_tool_calls"] == 90
    assert summary["efficiency_signals"]["unoutlined_tool_calls"] == 10


def test_optimizer_outline_preserves_event_containing_unicode_line_separator():
    events = [
        {"type": "turn_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "grep",
            "args": {"pattern": "before\u2028after"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "grep",
            "isError": False,
            "result": {"content": [{"type": "text", "text": "none"}]},
        },
    ]
    stdout = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)

    outline = operating_memory_optimizer._event_outline(stdout)

    assert len(outline) == 1
    assert outline[0]["arguments"]["pattern"] == "before\u2028after"


@pytest.mark.asyncio
async def test_threshold_job_invokes_optimizer_only_after_enough_new_runs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PI_OPERATING_MEMORY_DIR", str(tmp_path))
    records = [
        {
            "artifact_hash": f"trace-{index}",
            "request": {"user_id": "user-1"},
            "result": {},
        }
        for index in range(3)
    ]
    optimized = []

    monkeypatch.setattr(
        operating_memory_optimizer,
        "_settings",
        lambda: {
            "operation_threshold": 3,
            "max_traces_per_user": 2,
            "artifact_scan_limit": 10,
        },
    )
    monkeypatch.setattr(
        operating_memory_optimizer,
        "load_inference_runs",
        lambda operation, *, limit: records if operation == "pi_memory" else [],
    )

    async def optimize(user_id, selected, store):
        optimized.append((user_id, [item["artifact_hash"] for item in selected]))
        store.mark_processed([item["artifact_hash"] for item in selected])
        return {"user_id": user_id, "source_traces": len(selected)}

    monkeypatch.setattr(operating_memory_optimizer, "_optimize_user", optimize)

    result = await operating_memory_optimizer.run_operating_memory_threshold_job()

    assert result["optimized_users"] == 1
    assert optimized == [("user-1", ["trace-1", "trace-0"])]


@pytest.mark.asyncio
async def test_threshold_job_combines_write_and_search_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_OPERATING_MEMORY_DIR", str(tmp_path))
    records = {
        "pi_memory": [
            {
                "artifact_hash": "write-trace",
                "recorded_at": "2026-08-16T10:00:00Z",
                "request": {"user_id": "user-1"},
                "result": {},
            }
        ],
        "pi_memory_search": [
            {
                "artifact_hash": "search-trace",
                "recorded_at": "2026-08-16T11:00:00Z",
                "request": {"user_id": "user-1"},
                "result": {},
            }
        ],
    }
    monkeypatch.setattr(
        operating_memory_optimizer,
        "_settings",
        lambda: {
            "operation_threshold": 2,
            "max_traces_per_user": 2,
            "artifact_scan_limit": 10,
        },
    )
    monkeypatch.setattr(
        operating_memory_optimizer,
        "load_inference_runs",
        lambda operation, *, limit: records[operation],
    )
    optimized = []

    async def optimize(user_id, selected, store):
        optimized.append((user_id, [item["artifact_hash"] for item in selected]))
        return {"user_id": user_id, "source_traces": len(selected)}

    monkeypatch.setattr(operating_memory_optimizer, "_optimize_user", optimize)

    result = await operating_memory_optimizer.run_operating_memory_threshold_job()

    assert result["optimized_users"] == 1
    assert optimized == [("user-1", ["write-trace", "search-trace"])]


def test_optimizer_selection_keeps_high_signal_failure_with_recent_controls():
    records = [
        {
            "artifact_hash": f"normal-{index}",
            "request": {"record": "conversation"},
            "result": {"touched": ["Conversations/a.md"], "rounds": 2},
        }
        for index in range(10)
    ]
    records.append(
        {
            "artifact_hash": "older-loop",
            "request": {"record": "conversation"},
            "result": {
                "errors": ["Pi tool-round limit exceeded (48)"],
                "touched": [],
                "rounds": 49,
                "truncated": True,
            },
        }
    )

    selected = operating_memory_optimizer._select_optimizer_records(records, limit=4)
    hashes = [record["artifact_hash"] for record in selected]

    assert "older-loop" in hashes
    assert any(value.startswith("normal-") for value in hashes)
    assert len(hashes) == 4


@pytest.mark.asyncio
async def test_optimizer_records_candidate_system_event(tmp_path, monkeypatch):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    recorded_events = []
    invocation = {}

    monkeypatch.setattr(
        operating_memory_optimizer,
        "_settings",
        lambda: {"mode": "shadow"},
    )
    monkeypatch.setattr(
        operating_memory_optimizer,
        "_resolve_pi_config",
        lambda operation: SimpleNamespace(model="qwen", provider="local"),
    )

    async def invoke_pi(root, *, tool_handler, **kwargs):
        invocation.update(kwargs)
        tool_handler.dispatch(
            "propose_agents_memory",
            {
                "content": "# Candidate\n",
                "rationale": "Repeated trace behavior.",
                "evidence_ids": ["trace-a"],
            },
        )
        events = SimpleNamespace(
            stdout="",
            stderr="",
            summary="Recorded a shadow candidate.",
            errors=[],
            usage={},
            rounds=1,
            tool_calls=1,
            truncated=False,
            returncode=0,
        )
        return events, SimpleNamespace(call_count=1)

    monkeypatch.setattr(operating_memory_optimizer, "_invoke_pi", invoke_pi)
    monkeypatch.setattr(
        operating_memory_optimizer,
        "persist_inference_run",
        lambda **kwargs: ("request-a", "optimizer-a"),
    )
    monkeypatch.setattr(
        operating_memory_optimizer,
        "record_event_sync",
        lambda **event: recorded_events.append(event),
    )

    result = await operating_memory_optimizer._optimize_user(
        "user-1",
        [
            {
                "artifact_hash": "trace-a",
                "request": {},
                "result": {},
                "stdout": "",
            }
        ],
        store,
        component="agents",
        iteration_feedback={
            "rejected_guidance": "# Rejected\nDo not repeat calls.\n",
            "mechanical_regressions": ["new_fallback"],
        },
    )

    assert result["touched"] == ["candidates/"]
    schema_names = {schema["function"]["name"] for schema in invocation["schemas"]}
    assert "propose_agents_memory" in schema_names
    assert "write_skill_candidate" not in schema_names
    assert "evaluate AGENTS.md guidance only" in invocation["prompt"]
    assert "# Rejected" in invocation["prompt"]
    assert "new_fallback" in invocation["prompt"]
    assert invocation["config"].max_tokens == 8_192
    assert recorded_events == [
        {
            "severity": "info",
            "category": "memory",
            "source": "pi_operating_memory_optimizer",
            "title": "Pi operating-memory optimizer candidate generated",
            "detail": "Recorded a shadow candidate.",
            "user_id": "user-1",
            "metadata": {
                "mode": "shadow",
                "source_trace_count": 1,
                "touched": ["candidates/"],
                "optimizer_artifact_hash": "optimizer-a",
                "error_count": 0,
            },
        }
    ]


@pytest.mark.asyncio
async def test_optimizer_does_not_mark_source_traces_processed_after_failed_run(
    tmp_path, monkeypatch
):
    store = OperatingMemoryStore("user-1", root=tmp_path / "user-1")
    monkeypatch.setattr(
        operating_memory_optimizer,
        "_settings",
        lambda: {"mode": "shadow"},
    )
    monkeypatch.setattr(
        operating_memory_optimizer,
        "_resolve_pi_config",
        lambda operation: SimpleNamespace(model="qwen", provider="local"),
    )

    async def invoke_pi(root, *, tool_handler, **kwargs):
        events = SimpleNamespace(
            stdout="",
            stderr="context overflow",
            summary="",
            errors=["400 request exceeds available context size"],
            usage={},
            rounds=0,
            tool_calls=0,
            truncated=False,
            returncode=1,
        )
        return events, SimpleNamespace(call_count=0)

    monkeypatch.setattr(operating_memory_optimizer, "_invoke_pi", invoke_pi)
    monkeypatch.setattr(
        operating_memory_optimizer,
        "persist_inference_run",
        lambda **kwargs: ("request-a", "optimizer-a"),
    )
    monkeypatch.setattr(
        operating_memory_optimizer, "record_event_sync", lambda **event: None
    )

    result = await operating_memory_optimizer._optimize_user(
        "user-1",
        [{"artifact_hash": "trace-a", "request": {}, "result": {}, "stdout": ""}],
        store,
        component="agents",
    )

    assert result["processed"] is False
    assert store.load_state()["processed_artifact_hashes"] == []


@pytest.mark.asyncio
async def test_operating_memory_routes_require_review_before_activation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PI_OPERATING_MEMORY_DIR", str(tmp_path))

    async def run_blocking(function, *args, **kwargs):
        return function(*args, **kwargs)

    # This unit test covers review/promotion semantics; avoid retaining a managed
    # application worker after the isolated pytest event loop closes.
    monkeypatch.setattr(memory_routes, "_run_blocking", run_blocking)
    user = SimpleNamespace(user_id="user-1")
    store = OperatingMemoryStore("user-1")
    store.propose_agents(
        "# Reviewed guidance\n",
        rationale="Repeated trace behavior.",
        evidence_ids=["trace-a"],
    )
    candidate_id = store.list_candidates()[0]["candidate_id"]
    recorded_events = []

    async def record_event(**event):
        recorded_events.append(event)

    monkeypatch.setattr(memory_routes, "record_event", record_event)

    overview = await memory_routes.get_operating_memory(
        candidate_limit=100,
        revision_limit=100,
        current_user=user,
    )
    detail = await memory_routes.get_operating_memory_candidate(
        candidate_id, current_user=user
    )
    review = await memory_routes.review_operating_memory_candidate(
        candidate_id,
        memory_routes.OperatingMemoryReviewRequest(
            decision="approve",
            rationale="Frozen replay passed.",
            evidence_ids=["longmemeval-dev-a", "longmemeval-holdout-a"],
        ),
        current_user=user,
    )
    promoted = await memory_routes.promote_operating_memory_candidate(
        candidate_id, current_user=user
    )

    assert overview["active_agents"] == ""
    assert overview["candidates"][0]["status"] == "shadow_candidate"
    assert detail["content"] == "# Reviewed guidance\n"
    assert review["candidate"]["manifest"]["status"] == "approved"
    assert promoted["active_agents"] == "# Reviewed guidance\n"
    assert [event["severity"] for event in recorded_events] == ["info", "info"]
