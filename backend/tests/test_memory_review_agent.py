"""The read-only agent that reviews a write before the run is accepted."""

import json
from types import SimpleNamespace

import pytest

from backend.services.memory.agent import review_agent
from backend.services.memory.agent.review_agent import (
    MAX_DIFF_BYTES,
    added_lines,
    render_added,
    review_vault_write,
)

PERSON_NOTE = """## About
- On 2026-08-04, discussed a suction-style phone stand with [[blair]].

## Conversations
![[Conversations.base#Person]]
"""


def _tool_call(name, arguments):
    return SimpleNamespace(
        id=f"call-{name}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _message(tool_calls=None, content=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda: {"role": "assistant", "content": content},
    )


def _response(message):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _chat(monkeypatch, *responses):
    """Stub the tool loop with a scripted response per round; record the messages."""

    seen = []
    queue = list(responses)

    async def fake_chat(messages, **kwargs):
        seen.append(list(messages))
        return queue.pop(0)

    monkeypatch.setattr(review_agent, "async_chat_with_tools", fake_chat)
    return seen


def _write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_added_lines_reports_only_what_the_write_introduced():
    before = "## About\n- old fact\n"
    after = "## About\n- old fact\n- new fact\n"

    assert added_lines(before, after) == ["- new fact"]


def test_added_lines_treats_a_brand_new_note_as_all_added():
    assert added_lines("", "## About\n- a fact\n") == ["## About", "- a fact"]


def test_render_added_bounds_a_huge_write_and_says_so(tmp_path):
    _write(tmp_path, "People/Alex.md", "- x\n")
    _write(tmp_path, "Daily/2026-08-04.md", "- y\n" * 20_000)

    added, count = render_added(tmp_path, {}, ["People/Alex.md", "Daily/2026-08-04.md"])

    assert len(added) < MAX_DIFF_BYTES + 200
    assert "further notes omitted" in added
    # The count is of everything added, not of what fitted — a bounded view must not
    # under-report the size of the write it is bounding.
    assert count > 20_000


@pytest.mark.asyncio
async def test_a_redundant_bullet_the_reviewer_finds_becomes_a_finding(
    tmp_path, monkeypatch
):
    """The DeepSeek failure: a well-formed bullet re-recording a known fact.

    Structural verification passed on this write. Only reading the note the bullet
    landed in can decide it, which is why the reviewer is an agent.
    """

    _write(tmp_path, "People/alex.md", PERSON_NOTE)
    before = {"People/alex.md": PERSON_NOTE}
    _write(
        tmp_path,
        "People/alex.md",
        PERSON_NOTE.replace(
            "## Conversations",
            "- On 2026-08-04, called a magnetic phone stand cheap.\n\n## Conversations",
        ),
    )
    seen = _chat(
        monkeypatch,
        _response(_message([_tool_call("read_note", {"path": "People/alex.md"})])),
        _response(
            _message(
                [
                    _tool_call(
                        "report_findings",
                        {
                            "findings": [
                                {
                                    "path": "People/alex.md",
                                    "rule": "redundant",
                                    "detail": "already recorded as 'discussed a "
                                    "suction-style phone stand'",
                                }
                            ]
                        },
                    )
                ]
            )
        ),
    )

    result = await review_vault_write(
        tmp_path,
        source="Alex and Blair talked about a phone stand.",
        before=before,
        touched=["People/alex.md"],
        record="day",
    )

    assert result.reported is True
    assert [(f.path, f.rule) for f in result.findings] == [
        ("People/alex.md", "redundant")
    ]
    assert result.notes_read == ["People/alex.md"]
    # The reviewer is shown the added line and the source, never the writer's reasoning.
    task = seen[0][-1]["content"]
    assert "magnetic phone stand" in task and "<source" in task


@pytest.mark.asyncio
async def test_a_clean_write_produces_no_findings(tmp_path, monkeypatch):
    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- On 2026-08-06, adopted a cat.")
    _chat(
        monkeypatch,
        _response(_message([_tool_call("report_findings", {"findings": []})])),
    )

    result = await review_vault_write(
        tmp_path,
        source="Alex adopted a cat.",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert result.reported is True
    assert result.findings == []


@pytest.mark.asyncio
async def test_an_invented_rule_is_dropped_rather_than_repaired(tmp_path, monkeypatch):
    """Off-vocabulary rules are the model wandering into style advice.

    Passing one through would send the writer on a repair pass against a rule nothing
    in the system defines.
    """

    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")
    _chat(
        monkeypatch,
        _response(
            _message(
                [
                    _tool_call(
                        "report_findings",
                        {
                            "findings": [
                                {
                                    "path": "People/alex.md",
                                    "rule": "wording",
                                    "detail": "could be phrased more concisely",
                                },
                                {
                                    "path": "People/alex.md",
                                    "rule": "unsupported",
                                    "detail": "the source never mentions this",
                                },
                            ]
                        },
                    )
                ]
            )
        ),
    )

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert [f.rule for f in result.findings] == ["unsupported"]
    assert any("wording" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_a_reviewer_that_answers_in_prose_is_asked_for_the_verdict(
    tmp_path, monkeypatch
):
    """The analysis is in the prose; only the call that makes it actionable is missing.

    Discarding the review there throws away the whole thing over its last step.
    """

    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")
    seen = _chat(
        monkeypatch,
        _response(_message(None, content="The Tokyo line is not in the source.")),
        _response(
            _message(
                [
                    _tool_call(
                        "report_findings",
                        {
                            "findings": [
                                {
                                    "path": "People/alex.md",
                                    "rule": "unsupported",
                                    "detail": "the source never mentions Tokyo",
                                }
                            ]
                        },
                    )
                ]
            )
        ),
    )

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert result.reported is True
    assert [f.rule for f in result.findings] == ["unsupported"]
    # Its own analysis is carried into the ask, and there is nothing left to search
    # with — which is what makes the second call terminate instead of grepping again.
    assert "Tokyo line is not in the source" in str(seen[1])
    assert seen[1][-1]["content"].startswith("Stop searching.")


@pytest.mark.asyncio
async def test_a_forced_verdict_that_still_refuses_to_report_yields_nothing(
    tmp_path, monkeypatch
):
    """A review that cannot be acted on is the same as no review — never a block."""

    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")
    _chat(
        monkeypatch,
        _response(_message(None, content="Looks fine to me.")),
        _response(_message(None, content="I already told you it is fine.")),
    )

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert result.findings == []
    assert result.reported is False
    assert any("no report_findings call" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_a_reviewer_that_crashes_never_fails_the_write(tmp_path, monkeypatch):
    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(review_agent, "async_chat_with_tools", boom)

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert result.findings == []
    assert result.warnings


@pytest.mark.asyncio
async def test_the_round_cap_still_collects_what_the_reviewer_already_confirmed(
    tmp_path, monkeypatch
):
    """Measured on the live vault: six rounds ran out with the verdict already in prose.

    Running out of search budget is not a reason to discard a review — it is the reason
    to ask for one.
    """

    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")
    grep_forever = _response(_message([_tool_call("grep", {"pattern": "fact"})]))
    seen = _chat(
        monkeypatch,
        grep_forever,
        grep_forever,
        _response(
            _message(
                [
                    _tool_call(
                        "report_findings",
                        {
                            "findings": [
                                {
                                    "path": "People/alex.md",
                                    "rule": "redundant",
                                    "detail": "the note already says this",
                                }
                            ]
                        },
                    )
                ]
            )
        ),
    )

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
        max_rounds=2,
    )

    assert result.rounds == 3
    assert result.reported is True
    assert [f.rule for f in result.findings] == ["redundant"]
    assert any("round cap" in w for w in result.warnings)
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_a_write_that_added_nothing_is_not_sent_to_the_model(
    tmp_path, monkeypatch
):
    """A rename or a deletion has no added lines; spending an agent run on it is waste."""

    _write(tmp_path, "People/alex.md", PERSON_NOTE)

    async def never(*_args, **_kwargs):
        raise AssertionError("the reviewer must not be invoked with nothing to review")

    monkeypatch.setattr(review_agent, "async_chat_with_tools", never)

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert result.reported is True
    assert result.findings == []


@pytest.mark.asyncio
async def test_a_truncated_source_withdraws_the_unsupported_verdict(
    tmp_path, monkeypatch
):
    """Measured: a 39,563-char day digest cut at 24,000 hid the 20:48 gaming session.

    The reviewer then flagged a bullet about that session as `unsupported` — confidently,
    citing the media episode it could still see. A reviewer shown part of the source
    cannot tell "the source never said this" from "the source said it in the part you
    were not given", and deleting a true fact is the worst outcome this check can have.
    Redundancy is unaffected: that is judged against the vault, which it saw whole.
    """

    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")
    seen = _chat(
        monkeypatch,
        _response(
            _message(
                [
                    _tool_call(
                        "report_findings",
                        {
                            "findings": [
                                {
                                    "path": "People/alex.md",
                                    "rule": "unsupported",
                                    "detail": "I could not find this in the source",
                                },
                                {
                                    "path": "People/alex.md",
                                    "rule": "redundant",
                                    "detail": "the note already records this",
                                },
                            ]
                        },
                    )
                ]
            )
        ),
    )

    result = await review_vault_write(
        tmp_path,
        source="x" * (review_agent.MAX_SOURCE_BYTES + 1),
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert [f.rule for f in result.findings] == ["redundant"]
    assert any("truncated" in w for w in result.warnings)
    # It is also told, so a compliant model does not waste the verdict in the first place.
    assert "CANNOT conclude that anything is `unsupported`" in seen[0][-1]["content"]


@pytest.mark.asyncio
async def test_a_title_cased_path_is_resolved_to_the_note_that_exists(
    tmp_path, monkeypatch
):
    """Every measured run reported People/Alex.md for a note stored as alex.md.

    A finding that names a note nobody can open sends the repair pass at the wrong file.
    """

    _write(tmp_path, "People/alex.md", PERSON_NOTE + "- a new fact\n")
    _chat(
        monkeypatch,
        _response(
            _message(
                [
                    _tool_call(
                        "report_findings",
                        {
                            "findings": [
                                {
                                    "path": "People/Alex.md",
                                    "rule": "redundant",
                                    "detail": "the note already records this",
                                }
                            ]
                        },
                    )
                ]
            )
        ),
    )

    result = await review_vault_write(
        tmp_path,
        source="unrelated",
        before={"People/alex.md": PERSON_NOTE},
        touched=["People/alex.md"],
        record="day",
    )

    assert [f.path for f in result.findings] == ["People/alex.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["write_note", "search_images"])
async def test_freshness_assessment_rejects_tools_outside_readonly_boundary(
    tmp_path, monkeypatch, tool
):
    _chat(
        monkeypatch,
        _response(
            _message(
                [_tool_call(tool, {"path": "People/Alex.md", "content": "changed"})]
            )
        ),
    )
    result = await review_agent.assess_vault_context(
        tmp_path, task="Check semantic changes", schema={"type": "object"}
    )
    assert not result.reported
    assert result.warnings
    assert not (tmp_path / "People/Alex.md").exists()


@pytest.mark.asyncio
async def test_freshness_truncated_report_cannot_be_unaffected(tmp_path, monkeypatch):
    response = _response(
        _message([_tool_call("report_assessment", {"verdict": "unaffected"})])
    )
    response.choices[0].finish_reason = "length"
    _chat(monkeypatch, response)
    result = await review_agent.assess_vault_context(
        tmp_path, task="Check changes", schema={"type": "object"}
    )
    assert not result.reported
    assert result.assessment is None
    assert result.warnings


@pytest.mark.asyncio
async def test_freshness_rejects_assessment_with_unfinished_reads(
    tmp_path, monkeypatch
):
    _chat(
        monkeypatch,
        _response(
            _message(
                [
                    _tool_call("report_assessment", {"verdict": "unaffected"}),
                    _tool_call("read_note", {"path": "People/Alex.md"}),
                ]
            )
        ),
    )
    result = await review_agent.assess_vault_context(
        tmp_path, task="Check changes", schema={"type": "object"}
    )
    assert not result.reported
    assert result.warnings
