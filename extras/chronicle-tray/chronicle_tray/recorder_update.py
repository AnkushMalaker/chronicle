"""Update the ScreenPipe recorder from the fork's prebuilt rolling release.

Capture nodes run the recorder CLI as a service on both platforms; the binary
itself comes from the AnkushMalaker/screenpipe fork (branch `chronicle`), whose
CI publishes prebuilt tarballs under the rolling `chronicle-latest` release.
This module turns an update into download → verify → swap → restart, so a node
never needs a Rust toolchain.

The install keeps a stable path — `<state>/current/bin/screenpipe` — and points
the `screenpipe` symlink on PATH at it once. Updates replace `current/` (the
old one becomes `previous/`, so revert is a directory swap), which keeps the
path the service units captured at install time valid forever.

Runnable headless as well: `python -m chronicle_tray.recorder_update
check|install|revert` does the same thing the tray menu does.
"""

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RELEASE_BASE = (
    "https://github.com/AnkushMalaker/screenpipe/releases/download/chronicle-latest"
)
STATE_DIR = Path.home() / ".local/lib/screenpipe-cli-chronicle"
CURRENT = STATE_DIR / "current"
PREVIOUS = STATE_DIR / "previous"
INSTALLED_JSON = STATE_DIR / "installed.json"
PREVIOUS_JSON = STATE_DIR / "previous.json"
# Where the previous non-Chronicle install went when we took over the link
# (e.g. the npm CLI's real binary). Recorded so a revert past our own history
# is still possible by hand.
DISPLACED_JSON = STATE_DIR / "displaced.json"
DEFAULT_LINK = Path.home() / ".local/bin/screenpipe"
_TIMEOUT = 30


class RecorderUpdateError(RuntimeError):
    """A condition the menu should show verbatim, not a bug."""


def _asset_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if machine != "arm64":
            raise RecorderUpdateError("no prebuilt recorder for Intel macs")
        return "macos-aarch64"
    if sys.platform.startswith("linux"):
        if machine != "x86_64":
            raise RecorderUpdateError(f"no prebuilt recorder for linux/{machine}")
        return "linux-x86_64"
    raise RecorderUpdateError(f"unsupported platform {sys.platform}")


def fetch_manifest() -> dict:
    try:
        with urllib.request.urlopen(
            f"{RELEASE_BASE}/manifest.json", timeout=_TIMEOUT
        ) as r:
            manifest = json.load(r)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RecorderUpdateError(
                "no chronicle-latest release published yet"
            ) from error
        raise
    if _asset_key() not in manifest.get("assets", {}):
        raise RecorderUpdateError(f"release has no asset for {_asset_key()}")
    return manifest


def installed() -> dict | None:
    """Manifest snapshot of the build we installed, or None if the recorder
    on PATH is not ours (npm CLI, local cargo build, nothing at all)."""
    if not INSTALLED_JSON.exists():
        return None
    link = shutil.which("screenpipe")
    if link is None or Path(link).resolve() != (CURRENT / "bin/screenpipe").resolve():
        return None
    return json.loads(INSTALLED_JSON.read_text())


def check() -> tuple[dict | None, dict, bool]:
    """(installed, latest, update_available). An unmanaged install always
    counts as updatable — that is the migration path off the npm CLI."""
    latest = fetch_manifest()
    current = installed()
    return current, latest, current is None or current["commit"] != latest["commit"]


def _download_verified(manifest: dict, dest: Path) -> None:
    asset = manifest["assets"][_asset_key()]
    url = f"{RELEASE_BASE}/{asset['name']}"
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            digest.update(chunk)
            f.write(chunk)
    if digest.hexdigest() != asset["sha256"]:
        raise RecorderUpdateError("downloaded recorder failed its sha256 check")


def _take_over_link() -> Path:
    """Point the `screenpipe` on PATH at current/bin/screenpipe.

    A symlink (npm install, install-cli-local.sh) is repointed; a real binary
    is set aside into the state dir first. Its old target is recorded in
    displaced.json either way.
    """
    target = CURRENT / "bin/screenpipe"
    found = shutil.which("screenpipe")
    link = Path(found) if found else DEFAULT_LINK
    if link.resolve() == target.resolve():
        return link
    displaced: dict = {"link": str(link)}
    if link.is_symlink():
        displaced["target"] = os.readlink(link)
        link.unlink()
    elif link.exists():
        set_aside = STATE_DIR / f"displaced-{link.name}"
        shutil.move(link, set_aside)
        displaced["moved_to"] = str(set_aside)
    link.parent.mkdir(parents=True, exist_ok=True)
    DISPLACED_JSON.write_text(json.dumps(displaced, indent=1))
    tmp = link.parent / f".{link.name}.chronicle-tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    tmp.replace(link)
    return link


def _restart_recorder() -> None:
    """Restart the recorder service if it is installed and running; a node
    that has never installed the service just gets the new binary."""
    try:
        # Imported after the repo root joins sys.path, where clients.py lives.
        from chronicle_tray.paths import add_repo_root

        add_repo_root()
        # Imported after add_repo_root(), where the repo-root clients.py lives.
        import clients

        status = clients.component_status("screenpipe")
        if status["installed"] and status["active"]:
            clients.component_action("screenpipe", "restart")
    except Exception:
        logger.exception("recorder restart failed; restart it manually")


def install(manifest: dict | None = None) -> dict:
    """Download the latest build, swap it in, restart the service."""
    manifest = manifest or fetch_manifest()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="recorder-", dir=STATE_DIR))
    try:
        tarball = staging / "recorder.tar.gz"
        _download_verified(manifest, tarball)
        unpacked = staging / "unpacked"
        with tarfile.open(tarball) as tar:
            tar.extractall(unpacked, filter="data")
        binary = unpacked / "bin/screenpipe"
        if not binary.is_file():
            raise RecorderUpdateError("tarball has no bin/screenpipe")
        binary.chmod(0o755)
        # The swap itself: current → previous, unpacked → current. The service
        # keeps running on deleted inodes until the restart below.
        if PREVIOUS.exists():
            shutil.rmtree(PREVIOUS)
        if CURRENT.exists():
            CURRENT.replace(PREVIOUS)
            if INSTALLED_JSON.exists():
                INSTALLED_JSON.replace(PREVIOUS_JSON)
        unpacked.replace(CURRENT)
        INSTALLED_JSON.write_text(
            json.dumps(
                {**manifest, "installed_at": datetime.now(timezone.utc).isoformat()},
                indent=1,
            )
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _take_over_link()
    _restart_recorder()
    return manifest


def can_revert() -> bool:
    return (PREVIOUS / "bin/screenpipe").is_file()


def revert() -> dict | None:
    """Swap current/ and previous/ back and restart. Returns the manifest of
    the build now active, if known."""
    if not can_revert():
        raise RecorderUpdateError("no previous recorder build to revert to")
    swap = STATE_DIR / "swap"
    if swap.exists():
        shutil.rmtree(swap)
    CURRENT.replace(swap)
    PREVIOUS.replace(CURRENT)
    swap.replace(PREVIOUS)
    now_active = None
    if PREVIOUS_JSON.exists():
        now_active = json.loads(PREVIOUS_JSON.read_text())
        stash = INSTALLED_JSON.read_text() if INSTALLED_JSON.exists() else None
        INSTALLED_JSON.write_text(json.dumps(now_active, indent=1))
        if stash is not None:
            PREVIOUS_JSON.write_text(stash)
        else:
            PREVIOUS_JSON.unlink()
    _take_over_link()
    _restart_recorder()
    return now_active


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verb = sys.argv[1] if len(sys.argv) > 1 else "check"
    if verb == "check":
        current, latest, available = check()
        have = current["describe"] if current else "unmanaged or not installed"
        print(f"installed: {have}")
        print(f"latest:    {latest['describe']} (built {latest['built_at']})")
        print("update available" if available else "up to date")
        return 0
    if verb == "install":
        manifest = install()
        print(f"installed {manifest['describe']} ({manifest['commit'][:8]})")
        return 0
    if verb == "revert":
        manifest = revert()
        print(f"reverted to {manifest['describe'] if manifest else 'previous build'}")
        return 0
    print(f"usage: {sys.argv[0]} check|install|revert", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
