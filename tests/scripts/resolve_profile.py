#!/usr/bin/env python3
"""Resolve a test service profile from tests/profiles.yml into shell exports.

A profile says which backing services are real for a run. This resolver turns
that declaration into the environment the compose stack needs, and fails fast
with an actionable message when a real service's prerequisites are absent --
so a run never silently proceeds with an empty API key and reports the
resulting failures as test failures.

    eval "$(resolve_profile.py mock)"
    resolve_profile.py deepgram-openai --json

Exit codes:
    0  resolved
    2  unknown profile
    3  prerequisites missing (env var unset, or required service unreachable)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
MANIFEST = TESTS_DIR / "profiles.yml"

# Where tests/configs is mounted inside the backend and worker containers
# (see docker-compose-test.yml: ../../tests/configs:/app/test-configs:ro).
CONTAINER_CONFIG_DIR = "/app/test-configs"

_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} against the current environment."""
    return _ENV_REF.sub(
        lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
    )


def load_manifest() -> dict:
    if not MANIFEST.exists():
        sys.exit(f"error: {MANIFEST} not found")
    return yaml.safe_load(MANIFEST.read_text())


def resolve(name: str | None) -> tuple[str, dict]:
    manifest = load_manifest()
    profiles = manifest.get("profiles") or {}
    name = name or os.environ.get("PROFILE") or manifest.get("default") or "mock"

    if name not in profiles:
        known = ", ".join(sorted(profiles))
        print(f"error: unknown test profile '{name}'", file=sys.stderr)
        print(f"       known profiles: {known}", file=sys.stderr)
        sys.exit(2)

    return name, profiles[name] or {}


def check_prerequisites(name: str, profile: dict) -> None:
    """Fail loudly, and with the exact remedy, before any container starts."""
    problems: list[str] = []

    for var in profile.get("requires_env") or []:
        value = os.environ.get(var, "")
        if not value or re.search(r"your-.*-here", value, re.IGNORECASE):
            problems.append(
                f"  {var} is not set\n"
                f"      export {var}=...   (or set it in tests/setup/.env.test)"
            )

    for service in profile.get("requires_service") or []:
        url = _expand(service.get("url", ""))
        label = service.get("name", url)
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status >= 400:
                    problems.append(
                        f"  {label} returned HTTP {response.status} at {url}"
                    )
        except (urllib.error.URLError, OSError) as exc:
            problems.append(f"  {label} is unreachable at {url} ({exc})")

    if problems:
        real = ", ".join(profile.get("real") or []) or "none"
        print(
            f"\nerror: test profile '{name}' needs real services ({real}) "
            f"but its prerequisites are not met:\n",
            file=sys.stderr,
        )
        print("\n".join(problems), file=sys.stderr)
        print(
            "\nRun the stubbed profile instead with:  make test PROFILE=mock\n",
            file=sys.stderr,
        )
        sys.exit(3)


def as_exports(name: str, profile: dict) -> str:
    config = Path(profile["config"]).name
    lines = [
        f"export TEST_PROFILE={name}",
        f"export TEST_CONFIG_FILE={CONTAINER_CONFIG_DIR}/{config}",
    ]

    compose_profiles = profile.get("compose_profiles") or []
    args = " ".join(f"--profile {p}" for p in compose_profiles)
    lines.append(f"export TEST_COMPOSE_PROFILE_ARGS={args!r}")
    lines.append(f"export COMPOSE_PROFILES={','.join(compose_profiles)!r}")

    for key, value in (profile.get("env") or {}).items():
        lines.append(f"export {key}={str(value)!r}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        help="profile name (default: PROFILE env, else manifest default)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the resolved profile as JSON"
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="resolve without verifying prerequisites (used by targets that only need the config path)",
    )
    args = parser.parse_args()

    name, profile = resolve(args.profile)
    if not args.skip_checks:
        check_prerequisites(name, profile)

    if args.json:
        print(json.dumps({"name": name, **profile}, indent=2))
    else:
        print(as_exports(name, profile))


if __name__ == "__main__":
    main()
