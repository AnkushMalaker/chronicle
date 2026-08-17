"""Capture Chronicle's complete route/theme/viewport review matrix."""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = {"desktop": (1440, 1000), "phone": (390, 844)}
REDIRECTS = {"/conversations", "/conversations/:id"}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
            if match:
                values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def redact(value: str) -> str:
    value = re.sub(
        r"(token|password|secret|api[_-]?key)=[^&\s]+",
        r"\1=<redacted>",
        value,
        flags=re.I,
    )
    return re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\b", "<jwt-redacted>", value)[:800]


def inventory(path: Path) -> list[str]:
    routes = re.findall(
        r"<Route\b[^>]*\bpath=[{]?['\"]([^'\"]+)['\"]", path.read_text()
    )
    result = []
    for route in routes:
        if route == "*":
            continue
        route = "/" if route == "/" else f"/{route.lstrip('/')}"
        if route not in REDIRECTS and route not in result:
            result.append(route)
    if "/login" not in result:
        raise RuntimeError(f"could not inventory routes from {path}")
    return result


def slug(route: str) -> str:
    return (
        "main"
        if route == "/"
        else re.sub(r"[^a-z0-9]+", "-", route.strip("/").lower()).strip("-")
    )


def dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def start_diagnostics(page):
    console, page_errors, request_failures = [], [], []
    page.on(
        "console", lambda message: console.append((message.type, redact(message.text)))
    )
    page.on("pageerror", lambda error: page_errors.append(redact(str(error))))
    page.on(
        "requestfailed",
        lambda request: request_failures.append(
            redact(f"{request.method} {request.url} ({request.failure})")
        ),
    )

    def finish() -> dict:
        counts = Counter(console)
        warnings = [
            {"type": kind, "message": text, "count": count}
            for (kind, text), count in counts.items()
            if kind in {"warning", "error"}
        ]
        routine = [
            {"type": kind, "message": text, "count": count}
            for (kind, text), count in counts.items()
            if kind not in {"warning", "error"}
        ]
        failures = list(dict.fromkeys(request_failures))
        aborted = [item for item in failures if "ERR_ABORTED" in item]
        failures = [item for item in failures if item not in aborted]
        return {
            "console": {"warnings_errors": warnings, "routine": routine},
            "page_errors": list(dict.fromkeys(page_errors)),
            "request_failures": failures,
            "navigation_aborts": aborted,
            "clean": not warnings and not page_errors and not failures,
        }

    return finish


def authenticate(playwright, backend: str, env_file: Path, ignore_https: bool):
    env = read_env(env_file)
    email = os.getenv("ADMIN_EMAIL", env.get("ADMIN_EMAIL"))
    password = os.getenv("ADMIN_PASSWORD", env.get("ADMIN_PASSWORD"))
    if not email or not password:
        raise RuntimeError("ADMIN_EMAIL and ADMIN_PASSWORD are required")
    request = playwright.request.new_context(
        base_url=backend, ignore_https_errors=ignore_https
    )
    response = request.post(
        "/auth/jwt/login", form={"username": email, "password": password}
    )
    if not response.ok:
        raise RuntimeError(f"login failed with HTTP {response.status}")
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = request.get(
        "/api/conversations?limit=100&sort_by=created_at&sort_order=desc",
        headers=headers,
    )
    conversations = response.json().get("conversations", []) if response.ok else []
    candidates = [
        item
        for item in conversations
        if item.get("processing_status") == "completed"
        and isinstance(item.get("audio_total_duration"), (int, float))
        and item["audio_total_duration"] > 1
    ] or conversations
    if not candidates:
        raise RuntimeError("no real recording is available")
    recording = min(
        candidates, key=lambda item: item.get("audio_total_duration", 10**12)
    )
    response = request.get(
        f"/api/timeline/day?date={date.today().isoformat()}&timezone=UTC",
        headers=headers,
    )
    episodes = response.json().get("episodes", []) if response.ok else []
    if not episodes:
        raise RuntimeError("no real timeline episode is available")
    fixtures = {
        "id": str(recording.get("conversation_id") or recording.get("id")),
        "episodeId": str(episodes[0]["episode_id"]),
    }
    request.dispose()
    return token, fixtures


def actions_for(route: str) -> list[str | None]:
    actions: list[str | None] = [None]
    if route.startswith("/recordings/"):
        actions.append("detailed-summary")
    elif route in {"/plugins", "/data-audit"}:
        actions.append("second-tab")
    elif route == "/system":
        actions.append("documentation")
    return actions


def perform(page, action: str) -> None:
    if action == "detailed-summary":
        page.get_by_text("Detailed Summary", exact=False).first.click()
    elif action == "second-tab":
        page.get_by_role("tab").nth(1).click()
    elif action == "documentation":
        page.locator("summary").first.click()
    else:
        raise RuntimeError(f"interaction is not allowlisted: {action}")


def save_manifest(path: Path, entries: list[dict], metadata: dict) -> None:
    successful = sum(
        item["status"] in {"captured", "review-issue", "skipped-existing"}
        for item in entries
    )
    path.write_text(
        json.dumps(
            {
                **metadata,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "coverage": {
                    "entries": len(entries),
                    "successful": successful,
                    "failed": len(entries) - successful,
                },
                "routes": entries,
            },
            indent=2,
        )
        + "\n"
    )


def merge_targeted_entries(
    existing: list[dict], captured: list[dict], targeted_templates: set[str]
) -> list[dict]:
    """Replace selected route results while preserving the rest of a dated review."""
    preserved = [
        item
        for item in existing
        if item.get("route_template", item.get("route")) not in targeted_templates
    ]
    return preserved + captured


def wait_for_loading_indicators(page, timeout_ms: int) -> None:
    """Let API-backed sections settle without waiting on Vite's persistent sockets."""
    try:
        page.wait_for_function(
            "() => !document.querySelector('main .animate-spin')", timeout=timeout_ms
        )
    except Exception:
        # A still-spinning control is preserved as a review flag by the caller.
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://localhost")
    parser.add_argument("--backend-url", default="http://localhost:8000")
    parser.add_argument("--app-file", default="backends/advanced/webui/src/App.tsx")
    parser.add_argument("--env-file", default="backends/advanced/.env")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--only-route",
        action="append",
        default=[],
        help="Capture only this App.tsx route template; repeat for multiple routes",
    )
    parser.add_argument(
        "--ignore-https-errors", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--interactions", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--settle-timeout-ms",
        type=int,
        default=12_000,
        help="Maximum additional wait for page loading indicators to clear",
    )
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    all_templates = inventory(Path(args.app_file))
    requested_templates = set(args.only_route)
    unknown_templates = requested_templates.difference(all_templates)
    if unknown_templates:
        raise RuntimeError(
            "unknown --only-route value(s): " + ", ".join(sorted(unknown_templates))
        )
    templates = [
        template
        for template in all_templates
        if not requested_templates or template in requested_templates
    ]
    output = Path(
        args.output_dir or f"artifacts/screenshots/{date.today().isoformat()}"
    )
    print(
        json.dumps(
            {"base_url": base, "output_dir": str(output), "routes": templates}, indent=2
        )
    )
    if args.dry_run:
        return
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    previous = {}
    existing_entries: list[dict] = []
    if manifest.exists():
        old_manifest = json.loads(manifest.read_text())
        if old_manifest.get("base_url") == base:
            existing_entries = old_manifest.get("routes", [])
            if args.resume:
                previous = {
                    (
                        item.get("route"),
                        item.get("theme"),
                        item.get("viewport"),
                        item.get("action"),
                    ): item
                    for item in existing_entries
                }
    entries = []
    metadata = {
        "date": date.today().isoformat(),
        "base_url": base,
        "backend_url": args.backend_url,
    }

    def manifest_entries(captured: list[dict]) -> list[dict]:
        if not requested_templates:
            return captured
        return merge_targeted_entries(existing_entries, captured, requested_templates)

    with sync_playwright() as playwright:
        token, fixtures = authenticate(
            playwright, args.backend_url, Path(args.env_file), args.ignore_https_errors
        )
        routes = [
            (
                template,
                re.sub(
                    r":(id|episodeId)", lambda match: fixtures[match.group(1)], template
                ),
            )
            for template in templates
        ]
        browser = playwright.chromium.launch()
        for viewport, (width, height) in VIEWPORTS.items():
            for theme in ("light", "dark"):
                for template, route in routes:
                    actions = (
                        actions_for(route)
                        if args.interactions and viewport == "phone"
                        else [None]
                    )
                    for action in actions:
                        key = (route, theme, viewport, action)
                        old = previous.get(key)
                        if (
                            old
                            and old.get("status")
                            in {"captured", "review-issue", "skipped-existing"}
                            and Path(old.get("file", "")).is_file()
                        ):
                            old["status"] = "skipped-existing"
                            entries.append(old)
                            continue
                        context = browser.new_context(
                            viewport={"width": width, "height": height},
                            device_scale_factor=1,
                            ignore_https_errors=args.ignore_https_errors,
                        )
                        script = f"localStorage.setItem('theme', {theme!r});"
                        script += (
                            "localStorage.removeItem('root_token');"
                            if template == "/login"
                            else f"localStorage.setItem('root_token', {token!r});"
                        )
                        context.add_init_script(script)
                        page = context.new_page()
                        finish = start_diagnostics(page)
                        suffix = f"-{action}" if action else ""
                        path = (
                            output / f"{slug(template)}-{viewport}-{theme}{suffix}.png"
                        )
                        record = {
                            "route": route,
                            "route_template": template,
                            "theme": theme,
                            "viewport": viewport,
                            "action": action,
                            "file": str(path),
                        }
                        try:
                            page.goto(
                                f"{base}{route}",
                                wait_until="domcontentloaded",
                                timeout=60_000,
                            )
                            page.wait_for_timeout(3_000)
                            wait_for_loading_indicators(page, args.settle_timeout_ms)
                            body = " ".join(page.locator("body").inner_text().split())
                            main = (
                                " ".join(page.locator("main").inner_text().split())
                                if page.locator("main").count()
                                else body
                            )
                            if template != "/login" and "/login" in page.url:
                                raise RuntimeError(
                                    "protected route redirected to login"
                                )
                            if len(main) <= 20:
                                raise RuntimeError("blank page")
                            if (
                                "Application error" in body
                                or "Something went wrong" in body
                            ):
                                raise RuntimeError("error boundary rendered")
                            if action:
                                perform(page, action)
                                page.wait_for_timeout(1_000)
                                wait_for_loading_indicators(
                                    page, args.settle_timeout_ms
                                )
                            flags = []
                            loading = (
                                page.locator("main .animate-spin").count()
                                if page.locator("main").count()
                                else 0
                            )
                            scroll_width = page.evaluate(
                                "document.documentElement.scrollWidth"
                            )
                            if loading:
                                flags.append(
                                    f"{loading} unresolved loading indicator(s)"
                                )
                            if viewport == "phone" and scroll_width > width:
                                flags.append(
                                    f"horizontal overflow: {scroll_width}px content in {width}px viewport"
                                )
                            page.screenshot(
                                path=str(path),
                                full_page=True,
                                animations="disabled",
                                scale="css",
                            )
                            clipped = path.with_name(path.stem + "-clipped-390x844.png")
                            if viewport == "phone" and scroll_width > width:
                                page.screenshot(
                                    path=str(clipped),
                                    full_page=False,
                                    animations="disabled",
                                    scale="css",
                                )
                                record["clipped_file"] = str(clipped)
                            elif viewport == "phone" and clipped.exists():
                                clipped.unlink()
                            image_width, image_height = dimensions(path)
                            record.update(
                                {
                                    "status": "review-issue" if flags else "captured",
                                    "url": page.url,
                                    "dimensions": {
                                        "width": image_width,
                                        "height": image_height,
                                    },
                                    "viewport_dimensions": {
                                        "width": width,
                                        "height": height,
                                    },
                                    "review_flags": flags,
                                    "body_preview": body[:300],
                                }
                            )
                        except Exception as error:
                            record.update(
                                {
                                    "status": "failed",
                                    "reason": redact(str(error)),
                                    "url": page.url,
                                }
                            )
                        record["diagnostics"] = finish()
                        entries.append(record)
                        context.close()
                        save_manifest(
                            manifest,
                            manifest_entries(entries),
                            {**metadata, "dynamic_ids": fixtures},
                        )
                        print(
                            f"[{record['status']}] {viewport}/{theme} {route}{suffix}"
                        )
        browser.close()
    final_entries = manifest_entries(entries)
    save_manifest(manifest, final_entries, {**metadata, "dynamic_ids": fixtures})
    failures = [item for item in entries if item["status"] == "failed"]
    (output / "capture-report.md").write_text(
        "# Chronicle screenshot project review\n\n"
        f"- Entries: **{len(final_entries)}**\n- Failed: **{sum(item['status'] == 'failed' for item in final_entries)}**\n"
        f"- Console-clean: **{sum(item['diagnostics']['clean'] for item in final_entries)}**\n"
        f"- Review issues: **{sum(bool(item.get('review_flags')) for item in final_entries)}**\n\n"
        "See `manifest.json` for per-page diagnostics, dimensions, and review flags.\n"
    )
    if failures:
        raise SystemExit(f"review incomplete: {len(failures)} capture(s) failed")


if __name__ == "__main__":
    main()
