"""Capture arbitrary Chronicle dashboard routes with optional API authentication."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def parse_route(value: str) -> tuple[str, str]:
    try:
        name, route = value.split("=", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError("routes must be NAME=/path") from error
    if not name or not route.startswith("/"):
        raise argparse.ArgumentTypeError("routes must be NAME=/path")
    return name, route


def default_backend_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.hostname}:8000"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:5173")
    parser.add_argument("--backend-url")
    parser.add_argument("--env-file", default="backends/advanced/.env")
    parser.add_argument("--authenticate", action="store_true")
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Accept a self-signed development certificate for the browser and login request",
    )
    parser.add_argument("--output-dir", default="artifacts/screenshots")
    parser.add_argument("--route", type=parse_route, action="append", required=True)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            device_scale_factor=1,
            ignore_https_errors=args.ignore_https_errors,
        )

        if args.authenticate:
            env = env_values(Path(args.env_file))
            email = os.getenv("ADMIN_EMAIL", env.get("ADMIN_EMAIL"))
            password = os.getenv("ADMIN_PASSWORD", env.get("ADMIN_PASSWORD"))
            if not email or not password:
                raise RuntimeError("ADMIN_EMAIL and ADMIN_PASSWORD are required")
            request = playwright.request.new_context(
                base_url=args.backend_url or default_backend_url(base_url),
                ignore_https_errors=args.ignore_https_errors,
            )
            response = request.post(
                "/auth/jwt/login", form={"username": email, "password": password}
            )
            if not response.ok:
                raise RuntimeError(f"login failed with HTTP {response.status}")
            token = response.json()["access_token"]
            base_path = urlsplit(base_url).path.strip("/") or "root"
            page.add_init_script(
                f"localStorage.setItem({base_path!r} + '_token', {token!r})"
            )
            request.dispose()

        for name, route in args.route:
            page.goto(f"{base_url}{route}", wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                # Dashboards with persistent SSE/HMR connections may never become idle.
                pass
            page.wait_for_timeout(1_000)
            body = " ".join(page.locator("body").inner_text().split())
            if "/login" in page.url and route != "/login":
                raise RuntimeError(f"{route} redirected to login")
            if not body:
                raise RuntimeError(f"{route} rendered a blank page")
            path = output_dir / f"{name}.png"
            page.screenshot(path=str(path), full_page=True)
            print(f"saved {path} ({page.url})")

        browser.close()


if __name__ == "__main__":
    main()
