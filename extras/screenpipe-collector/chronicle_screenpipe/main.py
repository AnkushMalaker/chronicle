from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

import httpx

from .collector import Collector, Config


def config_dir() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "chronicle-screenpipe"


def state_dir() -> Path:
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state")) / "chronicle-screenpipe"


def load_config() -> Config:
    raw = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
    raw["screenpipe_dir"] = Path(raw["screenpipe_dir"]).expanduser()
    return Config(**raw)


def pair(args: argparse.Namespace) -> None:
    response = httpx.post(
        f"{args.backend.rstrip('/')}/api/device-input/pair",
        json={
            "code": args.code,
            "name": args.name or platform.node(),
            "platform": platform.system().lower(),
            "provider": "screenpipe",
            "capabilities": ["audio", "activity", "screen_context"],
        },
        timeout=30,
    )
    response.raise_for_status()
    paired = response.json()
    target = config_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / "config.json"
    path.write_text(
        json.dumps(
            {
                "backend_url": args.backend,
                "source_id": paired["source_id"],
                "token": paired["token"],
                "screenpipe_dir": str(Path(args.screenpipe_dir).expanduser()),
                "screenpipe_url": args.screenpipe_url,
                "screenpipe_token": args.screenpipe_token,
                "forward_audio": args.forward_audio,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    print(f"paired {paired['source_id']}; configuration saved to {path}")


def install_service() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to install the user service")
    project = Path(__file__).resolve().parents[1]
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "chronicle-screenpipe.service"
    unit.write_text(
        "[Unit]\nDescription=Chronicle ScreenPipe companion\nAfter=network-online.target screenpipe.service\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={uv} run --project {project} chronicle-screenpipe run\n"
        "Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
    print(f"installed and started {unit.name}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Chronicle companion for ScreenPipe")
    sub = parser.add_subparsers(dest="command", required=True)
    pair_parser = sub.add_parser("pair")
    pair_parser.add_argument("--backend", required=True)
    pair_parser.add_argument("--code", required=True)
    pair_parser.add_argument("--name")
    pair_parser.add_argument("--screenpipe-dir", default="~/.screenpipe")
    pair_parser.add_argument("--screenpipe-url", default="http://127.0.0.1:3030")
    pair_parser.add_argument(
        "--screenpipe-token",
        default=os.getenv("SCREENPIPE_API_KEY"),
        help="token used by ScreenPipe's authenticated local API",
    )
    pair_parser.add_argument(
        "--forward-audio",
        choices=("none", "output", "input", "both"),
        default="both",
        help="which locally captured ScreenPipe audio sources Chronicle receives",
    )
    sub.add_parser("run")
    sub.add_parser("install-service")
    args = parser.parse_args()
    if args.command == "pair":
        pair(args)
    elif args.command == "run":
        Collector(load_config(), state_dir()).run()
    else:
        install_service()


if __name__ == "__main__":
    main()
