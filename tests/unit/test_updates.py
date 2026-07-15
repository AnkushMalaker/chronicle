"""Tests for updates.py — node version reporting + self-update.

Exercises the git orchestration against scratch repos (a local "origin" plus a
clone standing in for a node checkout). No Docker and no network: service
restarts are stubbed out, so perform_update()'s checkout/rollback logic is
what's under test.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _stub_missing(name: str, attrs: dict):
    """Insert a minimal fake module under *name* if it isn't already importable."""
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        fake = MagicMock()
        for k, v in attrs.items():
            setattr(fake, k, v)
        sys.modules[name] = fake


# Stub third-party deps services.py needs that aren't in the bare test runner
# (same set as test_docker_image_versioning.py).
_stub_missing("dotenv", {"dotenv_values": lambda path: {}})
_stub_missing("rich", {})
_stub_missing("rich.console", {"Console": MagicMock})
_stub_missing("rich.markup", {"escape": lambda s: s})
_stub_missing("rich.table", {"Table": MagicMock})
_stub_missing("setup_utils", {"read_env_value": lambda *a, **kw: None})

import updates  # noqa: E402  (needs the stubs + sys.path above)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: Path, filename: str, message: str):
    (repo / filename).write_text(message)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@pytest.fixture()
def origin_and_clone(tmp_path, monkeypatch):
    """A local origin with one commit + tag v0.1.0, and a clone of it.

    updates.REPO_ROOT is pointed at the clone (the "node checkout").
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "test@test")
    _git(origin, "config", "user.name", "test")
    _commit(origin, "file.txt", "one")
    _git(origin, "tag", "v0.1.0")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@test")
    _git(clone, "config", "user.name", "test")

    monkeypatch.setattr(updates, "REPO_ROOT", clone)
    return origin, clone


class TestRepoVersion:
    def test_branch_checkout(self, origin_and_clone):
        _origin, clone = origin_and_clone
        v = updates.repo_version()
        assert v["describe"] == "v0.1.0"
        assert v["branch"] == "main"
        assert v["dirty"] is False
        assert v["commit"] == _git(clone, "rev-parse", "--short", "HEAD")

    def test_detached_and_dirty(self, origin_and_clone):
        _origin, clone = origin_and_clone
        _git(clone, "checkout", "--detach", "v0.1.0")
        (clone / "file.txt").write_text("local edit")
        v = updates.repo_version()
        assert v["branch"] is None
        assert v["dirty"] is True
        assert v["describe"].endswith("-dirty")


class TestCheckUpdate:
    def test_branch_mode_up_to_date(self, origin_and_clone):
        info = updates.check_update()
        assert info["target"]["kind"] == "branch"
        assert info["target"]["ref"] == "origin/main"
        assert info["update_available"] is False
        assert "error" not in info

    def test_branch_mode_behind(self, origin_and_clone):
        origin, _clone = origin_and_clone
        _commit(origin, "file.txt", "two")
        info = updates.check_update()
        assert info["update_available"] is True

    def test_branch_mode_ahead_only(self, origin_and_clone):
        """Local unpushed commits don't count as an available update."""
        _origin, clone = origin_and_clone
        _commit(clone, "local.txt", "local work")
        info = updates.check_update()
        assert info["update_available"] is False

    def test_release_mode_new_tag(self, origin_and_clone):
        origin, clone = origin_and_clone
        _git(clone, "checkout", "--detach", "v0.1.0")
        _commit(origin, "file.txt", "two")
        _git(origin, "tag", "v0.2.0")
        info = updates.check_update()
        assert info["target"] == {
            "ref": "v0.2.0",
            "kind": "tag",
            "commit": _git(origin, "rev-parse", "--short", "v0.2.0"),
        }
        assert info["update_available"] is True

    def test_semver_ordering_not_lexicographic(self, origin_and_clone):
        origin, clone = origin_and_clone
        _git(clone, "checkout", "--detach", "v0.1.0")
        for tag in ("v0.2.0", "v0.10.0", "v0.9.1"):
            _commit(origin, "file.txt", tag)
            _git(origin, "tag", tag)
        info = updates.check_update()
        assert info["target"]["ref"] == "v0.10.0"

    def test_explicit_target(self, origin_and_clone):
        origin, _clone = origin_and_clone
        _commit(origin, "file.txt", "two")
        _git(origin, "tag", "v0.2.0")
        _commit(origin, "file.txt", "three")
        _git(origin, "tag", "v0.3.0")
        info = updates.check_update(target="v0.2.0")
        assert info["target"]["ref"] == "v0.2.0"
        assert info["update_available"] is True

    def test_unknown_target_reports_error(self, origin_and_clone):
        info = updates.check_update(target="v9.9.9")
        assert "error" in info
        assert info["update_available"] is False


class TestPerformUpdate:
    def _quiet(self, monkeypatch):
        """Silence console output and stub the service-restart layer."""
        monkeypatch.setattr(updates.services, "console", MagicMock())

    def test_branch_pull(self, origin_and_clone, monkeypatch):
        origin, clone = origin_and_clone
        self._quiet(monkeypatch)
        _commit(origin, "file.txt", "two")
        ok = updates.perform_update(restart_services=False)
        assert ok is True
        assert (clone / "file.txt").read_text() == "two"
        # Still on the branch (pull, not a detached checkout).
        assert updates.repo_version()["branch"] == "main"

    def test_release_checkout(self, origin_and_clone, monkeypatch):
        origin, clone = origin_and_clone
        self._quiet(monkeypatch)
        _git(clone, "checkout", "--detach", "v0.1.0")
        _commit(origin, "file.txt", "two")
        _git(origin, "tag", "v0.2.0")
        ok = updates.perform_update(restart_services=False)
        assert ok is True
        assert updates.repo_version()["describe"] == "v0.2.0"

    def test_already_up_to_date_skips_restarts(self, origin_and_clone, monkeypatch):
        self._quiet(monkeypatch)
        restarts = MagicMock()
        monkeypatch.setattr(updates, "_restart_enabled_services", restarts)
        ok = updates.perform_update(restart_services=True)
        assert ok is True
        restarts.assert_not_called()

    def test_dirty_release_checkout_refused(self, origin_and_clone, monkeypatch):
        origin, clone = origin_and_clone
        self._quiet(monkeypatch)
        _git(clone, "checkout", "--detach", "v0.1.0")
        _commit(origin, "file.txt", "two")
        _git(origin, "tag", "v0.2.0")
        (clone / "file.txt").write_text("uncommitted")
        prev = _git(clone, "rev-parse", "HEAD")
        ok = updates.perform_update(restart_services=False)
        assert ok is False
        assert _git(clone, "rev-parse", "HEAD") == prev
        # The dirty tree is untouched.
        assert (clone / "file.txt").read_text() == "uncommitted"

    def test_rollback_on_service_failure(self, origin_and_clone, monkeypatch):
        origin, clone = origin_and_clone
        self._quiet(monkeypatch)
        _git(clone, "checkout", "--detach", "v0.1.0")
        _commit(origin, "file.txt", "two")
        _git(origin, "tag", "v0.2.0")
        prev = _git(clone, "rev-parse", "HEAD")

        monkeypatch.setattr(updates, "_enabled_services", lambda: ["backend"])
        monkeypatch.setattr(
            updates.services, "run_compose_command", lambda *a, **kw: False
        )
        ok = updates.perform_update()
        assert ok is False
        # Checkout was rolled back to the pre-update commit.
        assert _git(clone, "rev-parse", "HEAD") == prev

    def test_service_success_keeps_new_code(self, origin_and_clone, monkeypatch):
        origin, clone = origin_and_clone
        self._quiet(monkeypatch)
        _commit(origin, "file.txt", "two")

        calls = []
        monkeypatch.setattr(updates, "_enabled_services", lambda: ["backend"])
        monkeypatch.setattr(
            updates.services,
            "run_compose_command",
            lambda name, cmd, **kw: calls.append((name, cmd, kw)) or True,
        )
        ok = updates.perform_update()
        assert ok is True
        assert calls == [("backend", "up", {"build": True})]
        assert (clone / "file.txt").read_text() == "two"

    def test_prebuilt_uses_registry_env(self, origin_and_clone, monkeypatch):
        origin, _clone = origin_and_clone
        self._quiet(monkeypatch)
        _commit(origin, "file.txt", "two")
        monkeypatch.delenv("CHRONICLE_REGISTRY", raising=False)
        monkeypatch.delenv("CHRONICLE_TAG", raising=False)

        calls = []
        monkeypatch.setattr(updates, "_enabled_services", lambda: ["backend"])
        monkeypatch.setattr(
            updates.services,
            "run_compose_command",
            lambda name, cmd, **kw: calls.append(kw) or True,
        )
        ok = updates.perform_update(prebuilt="v0.2.0")
        assert ok is True
        # Prebuilt → no local build, registry env set for compose to consume.
        assert calls == [{"build": False}]
        import os

        assert os.environ["CHRONICLE_TAG"] == "v0.2.0"
        assert os.environ["CHRONICLE_REGISTRY"].startswith("ghcr.io/")
