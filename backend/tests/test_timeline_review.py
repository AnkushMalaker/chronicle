"""Timeline candidate diff and path safety contracts; application is covered by selective review tests."""

from pathlib import Path

import pytest

from backend.services.timeline import review


def test_potential_changes_capture_full_fenced_note_diff():
    changes = review.build_potential_changes(
        {
            "People/Ada.md": "old person note\n",
            "Topics/Removed.md": "obsolete\n",
        },
        {
            "People/Ada.md": "updated person note\n",
            "Topics/New.md": "new topic\n",
        },
    )

    assert [(item.note_path, item.operation) for item in changes] == [
        ("People/Ada.md", "update"),
        ("Topics/New.md", "create"),
        ("Topics/Removed.md", "delete"),
    ]
    assert changes[0].before_text == "old person note\n"
    assert changes[0].after_text == "updated person note\n"
    assert changes[0].before_hash != changes[0].after_hash


def test_potential_changes_keep_episode_provenance_out_of_note_text():
    changes = review.build_potential_changes(
        {"People/Ada.md": "old\n"},
        {"People/Ada.md": "new\n"},
        source_episode_keys_by_path={
            "People/Ada.md": ["episode-stable-a", "episode-stable-a"]
        },
    )

    assert changes[0].source_episode_keys == ["episode-stable-a"]
    assert "episode-stable-a" not in changes[0].after_text


def test_candidate_diff_does_not_mutate_the_accepted_vault(tmp_path: Path):
    live = tmp_path / "live"
    staged = tmp_path / "staged"
    (live / "People").mkdir(parents=True)
    (staged / "People").mkdir(parents=True)
    (live / "People/Ada.md").write_text("accepted\n", encoding="utf-8")
    (staged / "People/Ada.md").write_text("potential\n", encoding="utf-8")

    changes = review.build_potential_changes(
        review._snapshot(live), review._snapshot(staged)
    )

    assert len(changes) == 1
    assert (live / "People/Ada.md").read_text(encoding="utf-8") == "accepted\n"


def test_proposed_paths_cannot_escape_the_vault(tmp_path: Path):
    with pytest.raises(review.MemoryReviewError, match="Unsafe"):
        review._safe_note(tmp_path, "../outside.md")
