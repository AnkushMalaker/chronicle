"""launchd service management for the Chronicle wearable client on macOS."""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.chronicle.wearable-client"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "Chronicle"
LOG_FILE = LOG_DIR / "wearable-client.log"
APP_BUNDLE = Path.home() / "Applications" / "Chronicle Wearable.app"

PROJECT_DIR = Path(__file__).resolve().parent


def _find_uv() -> str:
    """Resolve the absolute path to the uv binary."""
    uv = shutil.which("uv")
    if uv:
        return uv
    # Common install locations
    for candidate in [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/opt/homebrew/bin/uv"),
    ]:
        if candidate.exists():
            return str(candidate)
    print("Error: could not find 'uv' binary. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh")
    sys.exit(1)



def _opus_dyld_path() -> str:
    """Get DYLD_LIBRARY_PATH for opuslib on macOS."""
    try:
        prefix = subprocess.check_output(
            ["brew", "--prefix", "opus"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        lib_dir = os.path.join(prefix, "lib")
        if os.path.isdir(lib_dir):
            return lib_dir
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return ""


def _create_app_bundle() -> None:
    """Create a .app bundle via osacompile so Raycast/Spotlight treat it as a real app."""
    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)

    # osacompile produces a proper Mach-O app bundle from AppleScript
    applescript = f'do shell script "launchctl kickstart gui/" & (do shell script "id -u") & "/{LABEL}"'
    result = subprocess.run(
        ["osacompile", "-o", str(APP_BUNDLE), "-e", applescript],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"osacompile failed: {result.stderr.strip()}")
        return

    # Patch Info.plist with our bundle metadata
    info_plist = APP_BUNDLE / "Contents" / "Info.plist"
    with open(info_plist, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleName": "Chronicle Wearable",
        "CFBundleDisplayName": "Chronicle Wearable",
        "CFBundleIdentifier": LABEL,
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": True,  # No dock icon
    })
    with open(info_plist, "wb") as f:
        plistlib.dump(info, f)


def _remove_app_bundle() -> None:
    """Remove the .app bundle."""
    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)
        print(f"Removed {APP_BUNDLE}")


def _build_plist() -> dict:
    """Build the launchd plist dictionary."""
    uv = _find_uv()
    opus_path = _opus_dyld_path()

    env = {}
    if opus_path:
        env["DYLD_LIBRARY_PATH"] = opus_path

    # Read .env file for backend config
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()

    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            uv, "run",
            "--project", str(PROJECT_DIR),
            "python", str(PROJECT_DIR / "main.py"), "menu",
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
    return plist


def install() -> None:
    """Install the launchd agent and load it."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist = _build_plist()

    # Unload existing if present
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
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Service '{LABEL}' installed and loaded.")
        print(f"Logs: {LOG_FILE}")
    else:
        print(f"launchctl bootstrap failed: {result.stderr.strip()}")
        print("Try: launchctl bootstrap gui/$(id -u) " + str(PLIST_PATH))

    _create_app_bundle()
    print(f"Created launcher app: {APP_BUNDLE}")
    print("You can now launch 'Chronicle Wearable' from Spotlight or Raycast.")


def uninstall() -> None:
    """Unload the launchd agent and remove the plist."""
    if not PLIST_PATH.exists():
        print(f"No plist found at {PLIST_PATH}")
        return

    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Service '{LABEL}' unloaded.")
    else:
        print(f"launchctl bootout: {result.stderr.strip()}")

    PLIST_PATH.unlink(missing_ok=True)
    print(f"Removed {PLIST_PATH}")
    _remove_app_bundle()


def kickstart() -> None:
    """Restart the launchd agent (e.g. after user quit the menu bar app)."""
    if not PLIST_PATH.exists():
        print(f"Service not installed. Run './start.sh install' first.")
        return

    result = subprocess.run(
        ["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Service '{LABEL}' started.")
    else:
        print(f"Failed to start: {result.stderr.strip()}")


def status() -> None:
    """Show the launchd service status."""
    if not PLIST_PATH.exists():
        print(f"Service not installed (no plist at {PLIST_PATH})")
        return

    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        # Extract key info from launchctl print output
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if any(k in stripped.lower() for k in ["state", "pid", "last exit", "runs"]):
                print(stripped)
    else:
        print(f"Service '{LABEL}' is not running.")
        print(f"Plist exists at: {PLIST_PATH}")


def logs(follow: bool = True) -> None:
    """Show or tail the service log file."""
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
        print(LOG_FILE.read_text()[-5000:])  # Last 5000 chars
