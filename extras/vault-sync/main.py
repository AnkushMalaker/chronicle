"""Chronicle Vault Sync — entry point with service-management subcommands."""

import argparse

_COMMANDS = ("menu", "install", "uninstall", "kickstart", "status", "logs")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Chronicle Vault Sync")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="Launch the menu bar app (default)")
    sub.add_parser("install", help="Install as a macOS login item")
    sub.add_parser("uninstall", help="Remove the macOS login item")
    sub.add_parser("kickstart", help="Relaunch the menu bar app")
    sub.add_parser("status", help="Show service status")
    sub.add_parser("logs", help="Tail service logs")

    args = parser.parse_args()
    command = args.command or "menu"

    if command == "menu":
        from menu_vault import main as menu_main

        menu_main()
    elif command == "install":
        from service import install

        install()
    elif command == "uninstall":
        from service import uninstall

        uninstall()
    elif command == "kickstart":
        from service import kickstart

        kickstart()
    elif command == "status":
        from service import status

        status()
    elif command == "logs":
        from service import logs

        logs()


if __name__ == "__main__":
    cli()
