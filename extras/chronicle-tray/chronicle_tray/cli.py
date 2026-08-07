"""Chronicle tray CLI — run the tray or manage its login service.

Service install/uninstall/restart delegates to the repo-root clients.py so the
unit definition lives in exactly one place (shared with ``services.py client``
and the node agent).
"""

import argparse
import subprocess
import sys
from pathlib import Path

from chronicle_tray.paths import add_repo_root

_MAC_LOG = Path.home() / "Library" / "Logs" / "Chronicle" / "tray.log"


def _clients():
    add_repo_root()
    # Imported after the repo root joins sys.path, where clients.py lives.
    import clients

    return clients


def main() -> None:
    parser = argparse.ArgumentParser(description="Chronicle desktop tray")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Run the tray in the foreground (default)")
    install = sub.add_parser("install", help="Install as a login service and start it")
    install.add_argument(
        "--pendant",
        action="store_true",
        help="Include BLE wearable streaming (installs the pendant extra)",
    )
    sub.add_parser("uninstall", help="Remove the login service")
    sub.add_parser("restart", help="Restart the login service")
    sub.add_parser("status", help="Show login service status")
    sub.add_parser("logs", help="Tail the service log")
    args = parser.parse_args()
    command = args.command or "run"

    if command == "run":
        # Imported here so `--help` and the subcommands below do not need PySide6.
        from chronicle_tray.app import run

        run()
        return

    clients = _clients()
    if command == "install":
        extras = ("pendant",) if args.pendant else ()
        clients.install_component("tray", extras)
        print("Tray installed and started (login service).")
        for check in clients.binary_checks():
            if not check["found"]:
                print(
                    f"note: {check['name']} not found ({check['needed_by']}) — {check['suggest']}"
                )
    elif command == "uninstall":
        clients.uninstall_component("tray")
        print("Tray login service removed.")
    elif command == "restart":
        ok = clients.component_action("tray", "restart")
        print("Tray restarted." if ok else "Restart failed — is it installed?")
    elif command == "status":
        status = clients.component_status("tray")
        state = (
            "not installed"
            if not status["installed"]
            else ("active" if status["active"] else "installed, inactive")
        )
        print(f"tray: {state}")
    elif command == "logs":
        if sys.platform == "darwin":
            subprocess.run(["tail", "-f", str(_MAC_LOG)], check=False)
        else:
            subprocess.run(
                ["journalctl", "--user", "-u", "chronicle-tray.service", "-f"],
                check=False,
            )


if __name__ == "__main__":
    main()
