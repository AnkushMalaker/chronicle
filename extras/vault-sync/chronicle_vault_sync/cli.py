"""Chronicle vault-sync — core library + legacy service management.

The menu bar / tray UI moved into the unified Chronicle tray
(extras/chronicle-tray), which imports this project's package in place. This CLI remains for managing (removing) a
pre-tray login service install.
"""

import argparse

from chronicle_vault_sync.service import logs, status, uninstall

_POINTER = (
    "The vault-sync tray moved into the unified Chronicle tray.\n"
    "  cd ../chronicle-tray && uv run chronicle-tray            # run it\n"
    "  cd ../chronicle-tray && uv run chronicle-tray install    # login service\n"
    "Your .env and vault pairing here are reused as-is."
)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Chronicle Vault Sync")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="(moved) the tray now lives in extras/chronicle-tray")
    sub.add_parser("install", help="(moved) install the unified tray instead")
    sub.add_parser("uninstall", help="Remove the legacy vault-sync login service")
    sub.add_parser("status", help="Show legacy service status")
    sub.add_parser("logs", help="Tail legacy service logs")

    args = parser.parse_args()
    command = args.command or "menu"

    if command in ("menu", "install"):
        raise SystemExit(_POINTER)
    if command == "uninstall":
        uninstall()
    elif command == "status":
        status()
    elif command == "logs":
        logs()


if __name__ == "__main__":
    cli()
