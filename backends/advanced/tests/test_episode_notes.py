"""Record notes for the bounds a day's analysis decided."""

from datetime import datetime, timezone
from types import SimpleNamespace

from advanced_omi_backend.services.timeline.episode_notes import (
    episode_note_path,
    render_episode_note,
    should_record,
    write_episode_notes,
)

ZONE = "Asia/Kolkata"


def episode(**overrides):
    defaults = dict(
        episode_id="ep-1",
        title="Standup",
        summary="Team sync about the release.",
        kind="meeting",
        salience="notable",
        conversational=True,
        entities=["Ankush"],
        assertions=[
            SimpleNamespace(role="user", confidence=0.91, claim="Ships Friday")
        ],
        related_conversation_ids=["conv-a"],
        evidence_refs=[],
        # Naive, as Mongo returns them.
        started_at=datetime(2026, 8, 6, 4, 5),
        ended_at=datetime(2026, 8, 6, 4, 35),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_naive_timestamps_are_utc_not_host_local():
    """04:05 UTC is 09:35 IST; reading it as node-local misfiles the whole note."""

    assert episode_note_path(episode(), ZONE) == "Episodes/2026-08-06 0935 Standup.md"


def test_title_characters_that_break_obsidian_or_windows_are_stripped():
    path = episode_note_path(episode(title="Sync: infra/deploy [P0] #urgent"), ZONE)

    assert path == "Episodes/2026-08-06 0935 Sync infra deploy P0 urgent.md"
    # One folder deep is the vault's rule; a '/' in a title would mint a nested folder
    # and verify_vault would reject the note.
    assert path.count("/") == 1


def test_only_conversational_or_standout_episodes_get_their_own_note():
    assert should_record(episode(conversational=True, salience="background"))
    assert should_record(episode(conversational=False, salience="highlight"))
    assert not should_record(episode(conversational=False, salience="routine"))


def test_note_carries_the_full_transcript_and_the_assertion_roles():
    body = render_episode_note(
        episode(), ZONE, {"conv-a": "Ankush: ships Friday."}, "2026-08-06"
    )

    assert "### Transcript" in body
    assert "Ankush: ships Friday." in body
    # role and confidence separate what the user said from media or app output; a
    # record note that drops them invites promoting the wrong thing into a fact.
    assert "[user · confidence 0.91] Ships Friday" in body
    assert "[[2026-08-06]]" in body
    assert 'episode_id: "ep-1"' in body


def test_evidence_ref_conversations_are_cited_alongside_related_ids():
    ref = SimpleNamespace(metadata={"conversation_id": "conv-b"})
    body = render_episode_note(
        episode(related_conversation_ids=["conv-a"], evidence_refs=[ref]),
        ZONE,
        {"conv-b": "second recording"},
        "2026-08-06",
    )

    assert "second recording" in body
    assert '- "conv-b"' in body


def test_write_skips_unrecordable_episodes_and_returns_written_paths(tmp_path):
    written = write_episode_notes(
        tmp_path,
        [
            episode(),
            episode(episode_id="ep-2", conversational=False, salience="routine"),
        ],
        ZONE,
        {},
        day_note_name="2026-08-06",
    )

    assert written == ["Episodes/2026-08-06 0935 Standup.md"]
    assert (tmp_path / written[0]).read_text(encoding="utf-8").startswith("---")


def test_an_unwritable_note_does_not_cost_the_day_its_memory(tmp_path, monkeypatch):
    """These notes are an addition to the day write, never a precondition for it."""

    def explode(self, *_args, **_kwargs):
        raise OSError("read-only vault")

    monkeypatch.setattr("pathlib.Path.write_text", explode)

    assert write_episode_notes(tmp_path, [episode()], ZONE, {}, "2026-08-06") == []


def test_timestamps_that_already_carry_a_zone_are_not_shifted_again():
    aware = episode(
        started_at=datetime(2026, 8, 6, 4, 5, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 6, 4, 35, tzinfo=timezone.utc),
    )

    assert episode_note_path(aware, ZONE) == "Episodes/2026-08-06 0935 Standup.md"
