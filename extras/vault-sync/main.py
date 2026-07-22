"""Chronicle desktop tray — entry point with service-management subcommands."""

import argparse
import sys

from service import install, kickstart, logs, status, uninstall

_COMMANDS = ("menu", "install", "uninstall", "kickstart", "status", "logs")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Chronicle Vault Sync")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="Launch the menu bar app (default)")
    sub.add_parser("install", help="Install as a desktop login service")
    sub.add_parser("uninstall", help="Remove the desktop login service")
    sub.add_parser("kickstart", help="Relaunch the menu bar app")
    sub.add_parser("status", help="Show service status")
    sub.add_parser("logs", help="Tail service logs")

    args = parser.parse_args()
    command = args.command or "menu"

    if command == "menu":
        if sys.platform == "darwin":
            from menu_vault import main as menu_main
        elif sys.platform.startswith("linux"):
            from menu_linux import main as menu_main
        else:
            raise SystemExit(f"unsupported desktop platform: {sys.platform}")

        menu_main()
    elif command == "install":
        install()
    elif command == "uninstall":
        uninstall()
    elif command == "kickstart":
        kickstart()
    elif command == "status":
        status()
    elif command == "logs":
        logs()


if __name__ == "__main__":
    cli()
