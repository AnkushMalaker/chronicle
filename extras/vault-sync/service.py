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
ROOT_ENV_FILE = PROJECT_DIR.parents[1] / ".env"
LOCAL_ENV_FILE = PROJECT_DIR / ".env"


def _dotenv_environment() -> dict[str, str]:
    """Load repo-wide settings, with vault-sync-specific values taking precedence."""
    env: dict[str, str] = {}
    for env_file in (ROOT_ENV_FILE, LOCAL_ENV_FILE):
        if env_file.exists():
            for key, value in dotenv_values(env_file).items():
                if value is not None:
                    env[key] = value
    return env


def _remove_app_bundle() -> None:
    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)
        print(f"Removed {APP_BUNDLE}")


def _build_plist() -> dict:
    uv = _find_uv()

    env = _dotenv_environment()
    # Ensure Homebrew bin is on PATH so the syncthing binary is found under launchd.
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

    return {
        "Label": LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "--project",
            str(PROJECT_DIR),
            "python",
            str(PROJECT_DIR / "main.py"),
            "menu",
        ],
        "WorkingDirectory": str(PROJECT_DIR),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "EnvironmentVariables": env,
    }


def install() -> None:
    if sys.platform.startswith("linux"):
        _linux_install()
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist = _build_plist()

    if PLIST_PATH.exists():
        print(f"Removing existing agent: {LABEL}")
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)],
            capture_output=True,
        )

    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    print(f"Wrote plist to {PLIST_PATH}")

    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Service '{LABEL}' installed and loaded.")
        print(f"Logs: {LOG_FILE}")
    else:
        print(f"launchctl bootstrap failed: {result.stderr.strip()}")
        print("Try: launchctl bootstrap gui/$(id -u) " + str(PLIST_PATH))

    _create_app_bundle()
    print(f"Created launcher app: {APP_BUNDLE}")


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


def _linux_install() -> None:
    uv = _find_uv()
    unit = _linux_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=Chronicle desktop tray\n"
        "After=graphical-session.target network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={PROJECT_DIR}\n"
        f"EnvironmentFile=-{ROOT_ENV_FILE}\n"
        f"EnvironmentFile=-{LOCAL_ENV_FILE}\n"
        f"ExecStart={uv} run --project {PROJECT_DIR} python {PROJECT_DIR / 'main.py'} menu\n"
        "Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=graphical-session.target\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", unit.name], check=True
    )
    # ``enable --now`` leaves an already-running unit untouched, so changes to
    # EnvironmentFile would not take effect until the next login.
    subprocess.run(["systemctl", "--user", "restart", unit.name], check=True)
    print(f"Installed and started {unit.name}")


def _linux_uninstall() -> None:
    unit = _linux_unit_path()
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", unit.name], check=False
    )
    unit.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Removed {unit}")


def _linux_systemctl(action: str) -> None:
    subprocess.run(
        ["systemctl", "--user", action, "chronicle-desktop.service"], check=False
    )
