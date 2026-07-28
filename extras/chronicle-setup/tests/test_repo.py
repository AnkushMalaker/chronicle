"""Locating the checkout.

The root cannot be derived from this package's own location — it is installed
into a virtualenv — so it is searched for, and a miss is an error rather than a
guess that would write .env files into an unrelated directory.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronicle_setup import repo


def _make_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for marker in repo._ROOT_MARKERS:
        (path / marker).write_text("")
    return path


def test_finds_root_from_the_working_directory(tmp_path, monkeypatch):
    root = _make_checkout(tmp_path / "checkout")
    monkeypatch.chdir(root)

    assert repo.find_repo_root() == root.resolve()


def test_finds_root_from_a_subdirectory(tmp_path, monkeypatch):
    root = _make_checkout(tmp_path / "checkout")
    deep = root / "extras" / "asr-services"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    assert repo.find_repo_root() == root.resolve()


def test_partial_marker_set_is_not_a_root(tmp_path):
    """A stray wizard.py somewhere must not be mistaken for the checkout."""
    almost = tmp_path / "almost"
    almost.mkdir()
    (almost / "wizard.py").write_text("")

    assert not repo.looks_like_repo_root(almost)


def test_missing_checkout_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHRONICLE_REPO_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="No Chronicle checkout"):
        repo.find_repo_root()


def test_explicit_override_wins(tmp_path, monkeypatch):
    root = _make_checkout(tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHRONICLE_REPO_ROOT", str(root))

    assert repo.find_repo_root() == root.resolve()


def test_bogus_override_raises_rather_than_falling_back(tmp_path, monkeypatch):
    """Silently ignoring the override would write config into the wrong tree."""
    _make_checkout(tmp_path / "real")
    monkeypatch.chdir(tmp_path / "real")
    monkeypatch.setenv("CHRONICLE_REPO_ROOT", str(tmp_path / "nope"))

    with pytest.raises(RuntimeError, match="not a Chronicle checkout"):
        repo.find_repo_root()
