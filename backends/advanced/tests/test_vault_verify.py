"""Post-write vault verification: what it catches, and what it must not."""

from advanced_omi_backend.services.memory.agent.vault_tools import VaultTools
from advanced_omi_backend.services.memory.vault_verify import (
    render_findings,
    verify_vault_changes,
)

PERSON_NOTE = """---
categories: ["[[People]]"]
---
## About
- Works on Chronicle.

## Conversations
![[Conversations.base#Person]]

## Mentions
- 2026-08-06 — mentioned the migration.
"""


def _snapshot(root):
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in root.rglob("*.md")
    }


def _write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_a_well_formed_new_note_produces_no_findings(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    assert verify_vault_changes(tmp_path, before) == []


def test_new_person_note_missing_the_aggregation_embed_is_reported(tmp_path):
    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "People/Vatsal.md",
        PERSON_NOTE.replace("![[Conversations.base#Person]]", ""),
    )

    findings = verify_vault_changes(tmp_path, before)

    assert [f.rule for f in findings] == ["note_schema"]
    # The finding must tell the model how to fix it, not just what is wrong.
    assert "Person Template.md" in findings[0].detail


def test_newly_duplicated_section_is_reported(tmp_path):
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE + "\n## About\n- pasted again\n")

    findings = verify_vault_changes(tmp_path, before)

    assert [f.rule for f in findings] == ["duplicate_section"]
    assert "'## about'" in findings[0].detail


def test_preexisting_duplication_is_not_blamed_on_this_run(tmp_path):
    """Otherwise a note that is already broken can never be repaired."""

    broken = PERSON_NOTE + "\n## About\n- duplicated before this run\n"
    _write(tmp_path, "People/Vatsal.md", broken)
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", broken + "- one genuinely new fact\n")

    assert verify_vault_changes(tmp_path, before) == []


def test_case_only_variant_of_an_existing_note_is_reported(tmp_path):
    _write(tmp_path, "People/hermes-labs.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Hermes-Labs.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    rules = [f.rule for f in findings]
    assert "case_collision" in rules
    detail = next(f.detail for f in findings if f.rule == "case_collision")
    assert "People/Hermes-Labs.md" in detail and "People/hermes-labs.md" in detail


def test_untouched_case_collision_is_not_reported(tmp_path):
    """Only problems this run introduced — an old pair is not this agent's to fix."""

    _write(tmp_path, "People/hermes-labs.md", PERSON_NOTE)
    _write(tmp_path, "People/Hermes-Labs.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "Topics/Chronicle.md",
        "## About\n- unrelated\n\n## Conversations\n![[Conversations.base#Topic]]\n",
    )

    assert verify_vault_changes(tmp_path, before) == []


def test_diarization_placeholder_person_note_is_reported(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Unknown Speaker 2.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    assert "not_a_person" in [f.rule for f in findings]


def test_hermes_is_a_topic_not_a_person(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Hermes.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    detail = next(f.detail for f in findings if f.rule == "not_a_person")
    assert "Topics/Hermes.md" in detail


def test_required_note_never_written_is_reported(tmp_path):
    """DeepSeek V4 Pro updated two People notes for a day and skipped the day note.

    It stopped after ten rounds with no error, no truncation and no stall — it simply
    believed it was finished, which is exactly what a self-reported checklist cannot
    catch and a filesystem check can.
    """

    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before, required=["Daily/2026-08-06.md"])

    assert [f.rule for f in findings] == ["record_missing"]
    assert findings[0].path == "Daily/2026-08-06.md"


def test_required_note_written_this_run_is_accepted(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "Daily/2026-08-06.md", "## 11:41 Standup\n- shipped it\n")

    assert (
        verify_vault_changes(tmp_path, before, required=["Daily/2026-08-06.md"]) == []
    )


def test_required_note_left_untouched_from_a_previous_run_is_reported(tmp_path):
    """A day note already on disk is not evidence that *this* run recorded the day."""

    _write(tmp_path, "Daily/2026-08-06.md", "## 09:00 Yesterday's write\n- old\n")
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before, required=["Daily/2026-08-06.md"])

    assert [f.rule for f in findings] == ["record_missing"]


def test_verify_vault_does_not_demand_the_record_note_from_a_run_that_touched_nothing(
    tmp_path,
):
    """An agent that judged the day already covered made a legitimate no-op.

    Demanding the record note there would turn a correct decision into a redundant
    write, which is the opposite of what the check is for.
    """

    tools = VaultTools(tmp_path, required_notes=["Daily/2026-08-06.md"])
    tools.baseline()

    assert "passed" in tools.verify_vault()

    tools.write_note("People/Vatsal.md", PERSON_NOTE)

    assert "Daily/2026-08-06.md" in tools.verify_vault()


def test_render_findings_is_actionable_and_says_so_when_clean(tmp_path):
    assert "passed" in render_findings([])

    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Unknown Speaker 1.md", PERSON_NOTE)
    text = render_findings(verify_vault_changes(tmp_path, before))

    assert "verify_vault again" in text
    assert "People/Unknown Speaker 1.md" in text


def test_read_note_windows_a_long_note_instead_of_returning_all_of_it(tmp_path):
    """An unbounded read is what pushed a 215 KB note through a 65k context."""

    _write(tmp_path, "Daily/2026-08-06.md", "\n".join(f"line {i}" for i in range(5000)))
    tools = VaultTools(tmp_path)

    first = tools.read_note("Daily/2026-08-06.md")

    assert "line 0" in first and "line 4999" not in first
    assert "of 5000]" in first
    assert len(first) < 20_000 + 500

    later = tools.read_note("Daily/2026-08-06.md", offset=4990)
    assert "line 4999" in later


def test_read_note_returns_a_short_note_whole_and_unadorned(tmp_path):
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    assert VaultTools(tmp_path).read_note("People/Vatsal.md") == PERSON_NOTE
