"""Chronological Timeline memory review safety contracts."""

from contextlib import nullcontext
from pathlib import Path

import pytest

from advanced_omi_backend.services.timeline import review


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


def test_apply_accepts_only_selected_changes_under_their_before_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "vault"
    (root / "People").mkdir(parents=True)
    (root / "People/Ada.md").write_text("accepted\n", encoding="utf-8")
    changes = review.build_potential_changes(
        {"People/Ada.md": "accepted\n"},
        {
            "People/Ada.md": "proposed\n",
            "Topics/Chronicle.md": "candidate topic\n",
        },
    )
    monkeypatch.setattr(review, "vault_run_lock", lambda _user_id: nullcontext())

    selected = [item for item in changes if item.note_path == "People/Ada.md"]
    applied = review._apply_changes_sync(
        "user-one",
        root,
        selected,
    )

    assert len(applied) == 1
    assert (root / "People/Ada.md").read_text(encoding="utf-8") == "proposed\n"
    assert not (root / "Topics/Chronicle.md").exists()


def test_apply_refuses_a_note_changed_since_the_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "vault"
    (root / "People").mkdir(parents=True)
    target = root / "People/Ada.md"
    target.write_text("accepted when generated\n", encoding="utf-8")
    change = review.build_potential_changes(
        {"People/Ada.md": "accepted when generated\n"},
        {"People/Ada.md": "potential\n"},
    )[0]
    target.write_text("human edit after generation\n", encoding="utf-8")
    monkeypatch.setattr(review, "vault_run_lock", lambda _user_id: nullcontext())

    with pytest.raises(review.MemoryReviewError, match="changed"):
        review._apply_changes_sync(
            "user-one",
            root,
            [change],
        )

    assert target.read_text(encoding="utf-8") == "human edit after generation\n"


def test_apply_allows_an_unrelated_note_to_change_since_the_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "vault"
    (root / "People").mkdir(parents=True)
    target = root / "People/Ada.md"
    target.write_text("accepted when generated\n", encoding="utf-8")
    before = review._snapshot(root)
    change = review.build_potential_changes(
        before,
        {"People/Ada.md": "potential\n"},
    )[0]
    (root / "Daily").mkdir()
    (root / "Daily/2026-08-24.md").write_text(
        "rolling timeline projection\n", encoding="utf-8"
    )
    monkeypatch.setattr(review, "vault_run_lock", lambda _user_id: nullcontext())

    applied = review._apply_changes_sync(
        "user-one",
        root,
        [change],
    )

    assert len(applied) == 1
    assert target.read_text(encoding="utf-8") == "potential\n"
    assert (root / "Daily/2026-08-24.md").read_text(encoding="utf-8") == (
        "rolling timeline projection\n"
    )


def test_proposed_paths_cannot_escape_the_vault(tmp_path: Path):
    with pytest.raises(review.MemoryReviewError, match="Unsafe"):
        review._safe_note(tmp_path, "../outside.md")
