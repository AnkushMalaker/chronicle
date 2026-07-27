"""Legacy login-service management for the pre-tray vault-sync app.

The tray moved to extras/chronicle-tray (installed via the repo-root
clients.py). Only uninstall/status/logs remain here, to manage installs
made before the move."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.chronicle.vault-sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "Chronicle"
LOG_FILE = LOG_DIR / "vault-sync.log"
APP_BUNDLE = Path.home() / "Applications" / "Chronicle Vault Sync.app"

PROJECT_DIR = Path(__file__).resolve().parent


def _remove_app_bundle() -> None:
    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)
        print(f"Removed {APP_BUNDLE}")


def uninstall() -> None:
    if sys.platform.startswith("linux"):
        _linux_uninstall()
        return
    if not PLIST_PATH.exists():
        print(f"No plist found at {PLIST_PATH}")
        return

    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Service '{LABEL}' unloaded.")
    else:
        print(f"launchctl bootout: {result.stderr.strip()}")

    PLIST_PATH.unlink(missing_ok=True)
    print(f"Removed {PLIST_PATH}")
    _remove_app_bundle()


def status() -> None:
    if sys.platform.startswith("linux"):
        _linux_systemctl("status")
        return
    if not PLIST_PATH.exists():
        print(f"Service not installed (no plist at {PLIST_PATH})")
        return
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if any(
                k in stripped.lower() for k in ["state", "pid", "last exit", "runs"]
            ):
                print(stripped)
    else:
        print(f"Service '{LABEL}' is not running.")


def logs(follow: bool = True) -> None:
    if sys.platform.startswith("linux"):
        args = ["journalctl", "--user", "-u", "chronicle-desktop.service"]
        args += ["-f"] if follow else ["-n", "100", "--no-pager"]
        subprocess.run(args, check=False)
        return
    if not LOG_FILE.exists():
        print(f"No log file at {LOG_FILE}")
        return
    if follow:
        print(f"Tailing {LOG_FILE} (Ctrl+C to stop)...")
        try:
            subprocess.run(["tail", "-f", str(LOG_FILE)])
        except KeyboardInterrupt:
            pass
    else:
        print(LOG_FILE.read_text()[-5000:])


def _linux_unit_path() -> Path:
    return Path.home() / ".config/systemd/user/chronicle-desktop.service"


def _linux_uninstall() -> None:
    unit = _linux_unit_path()
    subprocess.run(["systemctl", "--user", "disable", "--now", unit.name], check=False)
    unit.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Removed {unit}")


def _linux_systemctl(action: str) -> None:
    subprocess.run(
        ["systemctl", "--user", action, "chronicle-desktop.service"], check=False
    )
