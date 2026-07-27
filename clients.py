"""
Client-node components: registry + native unit management.

A Chronicle *client node* is a machine that only captures and streams data —
a desktop/laptop running the tray, the ScreenPipe collector, vault sync — with
no compose services and no GPU. These run as native user units (systemd user
services on Linux, launchd agents on macOS), not containers, so services.py's
compose machinery never touches them. This module is the single place that
knows how to install/inspect/restart them, shared by:

  - ``services.py client ...``            (CLI install/status/uninstall)
  - ``updates.py``                        (restart installed clients after a code update)
  - ``edge/service_manager.py``           (expose + control them from the hub WebUI)
  - the components' own CLIs              (e.g. ``chronicle-tray install``)

Stdlib-only on purpose: updates.py and the node agent must always be able to
import it, and client installs happen before any project venv exists.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

IS_MACOS = sys.platform == "darwin"

_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_MAC_LOG_DIR = Path.home() / "Library" / "Logs" / "Chronicle"

# name -> component config. Keys:
#   path         project dir relative to the repo root (a uv project)
#   description  human description (unit Description= / WebUI)
#   unit         systemd user unit name (Linux)
#   label        launchd label (macOS)
#   command      argv appended to ``uv run --project <path>`` in the unit
#   graphical    needs a desktop session (tray): graphical-session target /
#                launchd ProcessType Interactive
#   after_units  extra systemd After= ordering deps (Linux only)
CLIENT_COMPONENTS = {
    "tray": {
        "path": "extras/chronicle-tray",
        "description": "Chronicle desktop tray (vault sync, ScreenPipe, pendant)",
        "unit": "chronicle-tray.service",
        "label": "com.chronicle.tray",
        "command": ["chronicle-tray", "run"],
        "graphical": True,
    },
    "screenpipe-collector": {
        "path": "extras/screenpipe-collector",
        "description": "ScreenPipe → Chronicle forwarder (audio + app activity)",
        "unit": "chronicle-screenpipe.service",
        "label": "com.chronicle.screenpipe",
        "command": ["chronicle-screenpipe", "run"],
        "after_units": ["screenpipe.service"],
    },
}

# Superseded single-purpose units the unified tray replaces. Still restarted
# after updates while installed (they run from this checkout too); the vault
# ones are auto-removed when the tray is installed because two trays would
# fight over the same private Syncthing instance.
LEGACY_LINUX_UNITS = ["chronicle-desktop.service"]
LEGACY_MACOS_LABELS = ["com.chronicle.vault-sync", "com.chronicle.wearable-client"]
# Legacy units that hard-conflict with the tray (shared Syncthing home/ports).
_TRAY_CONFLICTS_LINUX = ["chronicle-desktop.service"]
_TRAY_CONFLICTS_MACOS = ["com.chronicle.vault-sync"]
# The pendant section replaces the wearable menu bar app (one BLE connection
# per device) — only a conflict when the tray is installed with that extra.
_PENDANT_CONFLICTS_MACOS = ["com.chronicle.wearable-client"]


def _find_uv() -> str:
    uv = shutil.which("uv")
    if uv:
        return uv
    for candidate in (
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/opt/homebrew/bin/uv"),
    ):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "uv not found — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    )


def _unit_path_env(uv_path: str) -> str:
    """PATH for units: user units get a minimal PATH, so pin one that resolves
    uv and the binaries components shell out to (syncthing, screenpipe)."""
    dirs = [
        str(Path(uv_path).parent),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return ":".join(seen)


def _exec_argv(name: str, extras=()) -> list[str]:
    cfg = CLIENT_COMPONENTS[name]
    uv = _find_uv()
    argv = [uv, "run", "--project", str(REPO_ROOT / cfg["path"])]
    for extra in extras:
        argv += ["--extra", extra]
    return argv + list(cfg["command"])


# ── systemd (Linux) ──────────────────────────────────────────────────────────


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def _systemd_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        result = _systemctl("is-system-running")
    except OSError:
        return False
    return (result.stdout or "").strip() in (
        "running",
        "degraded",
        "starting",
        "initializing",
        "maintenance",
        "stopping",
    )


def _install_linux(name: str, extras=()) -> None:
    cfg = CLIENT_COMPONENTS[name]
    if not _systemd_available():
        raise RuntimeError(
            "no systemd user instance — on WSL set systemd=true in /etc/wsl.conf"
        )
    uv = _find_uv()
    project = REPO_ROOT / cfg["path"]
    afters = ["network-online.target"] + list(cfg.get("after_units", []))
    wanted = "default.target"
    if cfg.get("graphical"):
        afters.insert(0, "graphical-session.target")
        wanted = "graphical-session.target"
    exec_start = " ".join(_exec_argv(name, extras))
    unit = _SYSTEMD_USER_DIR / cfg["unit"]
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\n"
        f"Description={cfg['description']}\n"
        + "".join(f"After={a}\n" for a in afters)
        + "\n[Service]\nType=simple\n"
        f"WorkingDirectory={project}\n"
        f"Environment=PATH={_unit_path_env(uv)}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\nRestartSec=5\n"
        f"\n[Install]\nWantedBy={wanted}\n"
    )
    subprocess.run(["loginctl", "enable-linger"], capture_output=True)
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", cfg["unit"])
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl enable --now {cfg['unit']} failed: {result.stderr.strip()}"
        )


def _uninstall_linux_unit(unit: str) -> None:
    _systemctl("disable", "--now", unit)
    (_SYSTEMD_USER_DIR / unit).unlink(missing_ok=True)
    _systemctl("daemon-reload")


# ── launchd (macOS) ──────────────────────────────────────────────────────────


def _plist_path(label: str) -> Path:
    return _LAUNCH_AGENTS_DIR / f"{label}.plist"


def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _dotenv_values(env_file: Path) -> dict:
    """Minimal KEY=VALUE parser (stdlib-only; unit env vars, not full dotenv)."""
    values = {}
    if not env_file.exists():
        return values
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _install_macos(name: str, extras=()) -> None:
    import plistlib

    cfg = CLIENT_COMPONENTS[name]
    label = cfg["label"]
    project = REPO_ROOT / cfg["path"]
    log_file = _MAC_LOG_DIR / f"{name}.log"
    _MAC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # launchd starts agents with no login env. The tray reads the repository-root
    # .env itself so edits take effect on restart instead of being copied into
    # (and potentially shadowed by) its plist. Other components retain their
    # project-local environment.
    env = {} if name == "tray" else _dotenv_values(project / ".env")
    env["PATH"] = _unit_path_env(_find_uv()) + ":" + os.environ.get("PATH", "")

    # The pendant (BLE) extra decodes Opus audio via opuslib, which loads the
    # native libopus through ctypes.util.find_library("opus"). That search does
    # not include Homebrew's lib dir, and launchd hands the agent a bare
    # environment, so point dyld's fallback search at the Homebrew prefixes
    # (Apple Silicon + Intel). Harmless when the dirs are absent.
    if "pendant" in extras:
        env["DYLD_FALLBACK_LIBRARY_PATH"] = "/opt/homebrew/lib:/usr/local/lib"

    plist = {
        "Label": label,
        "ProgramArguments": _exec_argv(name, extras),
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "EnvironmentVariables": env,
    }
    if cfg.get("graphical"):
        plist["ProcessType"] = "Interactive"

    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap {label} failed: {result.stderr.strip()}"
        )


def _uninstall_macos_label(label: str) -> None:
    path = _plist_path(label)
    if path.exists():
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
    path.unlink(missing_ok=True)


# ── public API ───────────────────────────────────────────────────────────────


def component_installed(name: str) -> bool:
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        return _plist_path(cfg["label"]).exists()
    return (_SYSTEMD_USER_DIR / cfg["unit"]).exists()


def component_active(name: str) -> bool:
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        return _launchctl("print", f"gui/{os.getuid()}/{cfg['label']}").returncode == 0
    return _systemctl("is-active", cfg["unit"]).returncode == 0


def component_status(name: str) -> dict:
    installed = component_installed(name)
    return {
        "name": name,
        "description": CLIENT_COMPONENTS[name]["description"],
        "installed": installed,
        "active": component_active(name) if installed else False,
    }


def installed_components() -> list[str]:
    return [name for name in CLIENT_COMPONENTS if component_installed(name)]


def install_component(name: str, extras=()) -> None:
    """Install (or reinstall) a component as a login user unit and start it.

    Installing the tray removes the superseded single-purpose tray units it
    replaces — two trays would race for the same private Syncthing instance.
    Raises RuntimeError with a human-readable reason on failure.
    """
    if name not in CLIENT_COMPONENTS:
        raise RuntimeError(f"Unknown client component: {name}")
    if name == "tray":
        conflicts = _TRAY_CONFLICTS_MACOS if IS_MACOS else _TRAY_CONFLICTS_LINUX
        if "pendant" in extras and IS_MACOS:
            conflicts = conflicts + _PENDANT_CONFLICTS_MACOS
        for legacy in conflicts:
            if _legacy_installed(legacy):
                _remove_legacy(legacy)
                print(f"Removed superseded unit {legacy} (replaced by the tray)")
    if IS_MACOS:
        _install_macos(name, extras)
    else:
        _install_linux(name, extras)


def uninstall_component(name: str) -> None:
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        _uninstall_macos_label(cfg["label"])
    else:
        _uninstall_linux_unit(cfg["unit"])


def component_action(name: str, action: str) -> bool:
    """start | stop | restart an installed component. Returns success."""
    cfg = CLIENT_COMPONENTS[name]
    if IS_MACOS:
        domain = f"gui/{os.getuid()}"
        if action == "stop":
            return (
                _launchctl("bootout", domain, str(_plist_path(cfg["label"]))).returncode
                == 0
            )
        if action == "start":
            return (
                _launchctl(
                    "bootstrap", domain, str(_plist_path(cfg["label"]))
                ).returncode
                == 0
            )
        return _launchctl("kickstart", "-k", f"{domain}/{cfg['label']}").returncode == 0
    if action not in ("start", "stop", "restart"):
        raise RuntimeError(f"Unknown action: {action}")
    return _systemctl(action, cfg["unit"]).returncode == 0


# ── legacy units (pre-unified-tray installs) ─────────────────────────────────


def _legacy_installed(unit_or_label: str) -> bool:
    if IS_MACOS:
        return _plist_path(unit_or_label).exists()
    return (_SYSTEMD_USER_DIR / unit_or_label).exists()


def _remove_legacy(unit_or_label: str) -> None:
    if IS_MACOS:
        _uninstall_macos_label(unit_or_label)
    else:
        _uninstall_linux_unit(unit_or_label)


def _restart_legacy(unit_or_label: str) -> bool:
    if IS_MACOS:
        return (
            _launchctl(
                "kickstart", "-k", f"gui/{os.getuid()}/{unit_or_label}"
            ).returncode
            == 0
        )
    return _systemctl("restart", unit_or_label).returncode == 0


def installed_legacy_units() -> list[str]:
    legacy = LEGACY_MACOS_LABELS if IS_MACOS else LEGACY_LINUX_UNITS
    return [u for u in legacy if _legacy_installed(u)]


# ── update integration ───────────────────────────────────────────────────────


def restart_installed(progress=None) -> list[tuple[str, bool]]:
    """Restart every installed client unit so it picks up updated code.

    Used by updates.perform_update() after the checkout moves: the units all
    ``uv run`` straight from this checkout, so a restart is all an update
    needs. Best-effort by design — a tray that fails to relaunch must not fail
    (or roll back) a node update. Returns [(unit, ok)].
    """
    progress = progress or (lambda msg: None)
    results: list[tuple[str, bool]] = []
    for name in installed_components():
        progress(f"Restarting {name}…")
        results.append((name, component_action(name, "restart")))
    for legacy in installed_legacy_units():
        progress(f"Restarting {legacy}…")
        results.append((legacy, _restart_legacy(legacy)))
    return results


# ── companion binaries ───────────────────────────────────────────────────────


def binary_checks() -> list[dict]:
    """Presence of the external binaries client components rely on, with an
    install suggestion for anything missing."""
    if IS_MACOS:
        syncthing_hint = "brew install syncthing"
    elif shutil.which("pacman"):
        syncthing_hint = "sudo pacman -S syncthing"
    else:
        syncthing_hint = "sudo apt install syncthing"
    checks = [
        {
            "name": "screenpipe",
            "needed_by": "screenpipe-collector",
            "found": shutil.which("screenpipe") is not None,
            "suggest": "install ScreenPipe: curl -fsSL get.screenpi.pe/cli | sh "
            "(see https://screenpi.pe), then `screenpipe service install`",
        },
        {
            "name": "syncthing",
            "needed_by": "tray (vault sync)",
            "found": shutil.which("syncthing") is not None,
            "suggest": syncthing_hint,
        },
    ]
    return checks
