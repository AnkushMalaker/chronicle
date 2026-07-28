"""Locating the checkout — including where there isn't one."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chronicle_client.config as config


def test_finds_a_checkout_by_marker_files(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    pkg = root / "extras" / "chronicle-client" / "chronicle_client"
    pkg.mkdir(parents=True)
    for marker in config._ROOT_MARKERS:
        (root / marker).write_text("")
    # Point __file__ into the fake checkout; otherwise the walk finds the real
    # repository this test is running from, before it ever consults the cwd.
    monkeypatch.setattr(config, "__file__", str(pkg / "config.py"))
    monkeypatch.chdir(root)

    assert config._find_repo_root() == root.resolve()


def test_marker_lookup_beats_counting_parents(tmp_path, monkeypatch):
    """A regular (non-editable) install lands in site-packages, where counting
    parent directories silently yields the wrong root and an empty API key."""
    root = tmp_path / "checkout"
    root.mkdir()
    for marker in config._ROOT_MARKERS:
        (root / marker).write_text("")
    deep = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "chronicle_client"
    deep.mkdir(parents=True)
    monkeypatch.setattr(config, "__file__", str(deep / "config.py"))
    monkeypatch.chdir(root)

    assert config._find_repo_root() == root.resolve()


def test_shallow_install_path_does_not_raise(tmp_path, monkeypatch):
    """In the relay's image the package is copied to /chronicle-client, which has
    fewer than four parents. Indexing parents[3] blindly raised IndexError at
    import time and crash-looped the container."""
    shallow = tmp_path / "chronicle-client" / "chronicle_client"
    shallow.mkdir(parents=True)
    monkeypatch.setattr(config, "__file__", str(shallow / "config.py"))
    # Somewhere with no checkout above it, like a container's filesystem.
    monkeypatch.chdir(tmp_path)

    root = config._find_repo_root()  # must not raise
    assert isinstance(root, Path)


def test_explicit_override_wins(tmp_path, monkeypatch):
    root = tmp_path / "elsewhere"
    root.mkdir()
    for marker in config._ROOT_MARKERS:
        (root / marker).write_text("")
    monkeypatch.setenv("CHRONICLE_REPO_ROOT", str(root))

    assert config._find_repo_root() == root.resolve()


def test_bogus_override_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRONICLE_REPO_ROOT", str(tmp_path / "nope"))
    assert config._find_repo_root() != (tmp_path / "nope")
