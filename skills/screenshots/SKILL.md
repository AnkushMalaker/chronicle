---
name: screenshots
description: Captures consistent Chronicle web or Expo screenshots with Playwright, including authenticated and dynamic routes, full-project light/dark desktop/phone reviews, safe interaction passes, browser-console diagnostics, responsive overflow checks, and resumable dated reports. Use when documenting UI, reviewing visual regressions, auditing every page, or asked to screenshot all screens.
---

# Screenshot skill

Capture screenshots from a running app; do not add Playwright or other browser tooling to a project dependency file for a one-off capture. Use the repository rule:

```bash
uv run --with playwright python - <<'PY'
from playwright.sync_api import sync_playwright
print("browser automation runs ephemerally")
PY
```

## Playwright with uv

Use `uv run --with playwright` for Python browser automation. If Chromium is not already available, install it ephemerally with `uvx --from playwright playwright install chromium`; do not add Playwright to `package.json` or a Python project environment. Read `CLAUDE.local.md` and prefer the deployment's stable same-origin Caddy/HTTPS endpoint. Use direct Vite at `http://localhost:5173`, never `127.0.0.1`, only when no proxy is available.

Use a 1440×1000 viewport, a fixed device scale factor, `full_page=True`, and wait for `domcontentloaded` plus any page-specific loading indicator (Vite's HMR socket means `networkidle` never fires against the dev server). Save images to `artifacts/screenshots/` unless the task requests a committed asset. For a LAN deployment with a self-signed development certificate, pass `--ignore-https-errors`; never use it for an untrusted public host.

## Chronicle authentication

The web dashboard is protected by a JWT. Prefer UI login when testing authentication. For screenshot-only work, an API login is faster:

1. Read `ADMIN_EMAIL` and `ADMIN_PASSWORD` from `backends/advanced/.env` or the process environment without printing them.
2. `POST {backend}/auth/jwt/login` as form fields `username` and `password`.
3. Before navigating to a protected route, inject the returned `access_token` into local storage. The key is `{base-path-or-root}_token` (`root_token` for the normal `/` base path).
4. Load the route and verify it did not redirect to `/login`. The app verifies the token through `GET /users/me`.

Never put a token, password, or authenticated screenshot with personal data in a commit.

## Dashboard workflow

1. Start the dashboard and backend using the repository’s normal commands. Confirm the actual URL and port.
2. Read `backends/advanced/webui/src/App.tsx` and build the route inventory from its current `<Route>` entries. Exclude wildcard fallbacks and redirect-only aliases; do not copy a static inventory into the capture script.
3. Use a real recording ID for `/recordings/:id` and a real episode ID for `/timeline/:episodeId`; discover them through the UI or an authenticated API request. Never invent an ID and call that page complete.
4. Vite's HMR socket keeps the page from ever reaching `networkidle`. Wait on `domcontentloaded` plus the page's own content, not on network quiet. Never reject a page merely because its text contains “Loading”; detect blank/error pages and record unresolved loading indicators as review findings.
5. Capture `/login` unauthenticated. Log in through the UI, then capture every protected route with the same viewport, theme, and browser state.
6. Wait for the page heading/content and loading indicators to settle. Use `full_page=True`, and save predictable names such as `01-login.png`, `02-recordings.png`, and `03-recording-detail-<id>.png`.
7. Record failures separately. A redirect to login, error boundary, blank page, or missing fixture is a failed capture—not a screenshot of the requested page. Preserve substantially rendered pages with unresolved subsections as review issues.

## Full-project review

For “all pages/screens” requests, use the reusable project-review script. It inventories `App.tsx`, discovers real dynamic IDs, captures desktop 1440×1000 and phone 390×844 in both themes, performs allowlisted non-destructive phone interactions, records isolated browser diagnostics, detects horizontal overflow, and writes an incremental manifest plus report into a dated directory.

```bash
uv run --with playwright python \
  skills/screenshots/scripts/screenshot_project_review.py \
  --base-url https://localhost \
  --backend-url http://localhost:8000 \
  --ignore-https-errors
```

Before a long or delegated run:

1. Perform read-only health checks; do not restart or rebuild services for a screenshot audit.
2. Capture and visually inspect the user's named priority pages manually as a smoke test.
3. Preview the dynamic route inventory with `--dry-run`.
4. Start the matrix only after the smoke captures render correctly.
5. When delegating, keep the smoke test and final validation with the primary agent. Monitor for five minutes by checking agent status, the active Playwright process, newly written files, and manifest progress. Do not wait blindly.

The review is resumable by default. Existing successful files referenced by the manifest are validated and skipped; use `--no-resume` for an intentional clean recapture. Derive filenames before capture; never fall back to shared `retry-*.png` names.

The harness waits for visible loading indicators after its initial render delay, up to 12 seconds by default. Use `--settle-timeout-ms` when a known external-service query needs a different ceiling; a spinner that remains after that timeout is retained as a review finding.

After fixing a subset of pages, replace only those route results while retaining the rest of the dated review. Repeat `--only-route` for each `App.tsx` route template and pair it with `--no-resume`:

```bash
uv run --with playwright python \
  skills/screenshots/scripts/screenshot_project_review.py \
  --base-url https://localhost \
  --backend-url http://localhost:8000 \
  --only-route /system \
  --only-route /system-errors \
  --no-resume \
  --ignore-https-errors
```

Treat a substantially rendered page with a stuck subsection as `review-issue`, preserve its screenshot, and record its loading indicators. Reserve `failed` for a missing or blank page, login redirect, error boundary, missing real fixture, navigation failure, or missing output.

For phone captures, compare `document.documentElement.scrollWidth` with the 390px viewport. When content overflows, retain the full-page screenshot as evidence and add a clipped 390×844 screenshot for realistic phone review.
When a recapture no longer overflows, remove its previously generated clipped companion so the dated directory cannot retain stale regression evidence.

Only perform explicitly allowlisted, reversible interactions such as opening tabs, disclosures, summaries, and transcripts. Never submit forms or trigger save, upload, record, delete, enqueue, train, restart, provider-switch, or similar actions.

If the app becomes unavailable, follow `AGENTS.md` under “Investigating unexplained service restarts”: check System Events filtered to `service`, correlate operation IDs and logs, and avoid labeling the event a crash without evidence.

Each result must record console warnings/errors, page errors, failed requests, navigation-aborted requests, viewport and PNG dimensions, theme, route, action, and review flags. Redact credentials and tokens. Keep diagnostics isolated per page/context.

Use the generic framework for any set of public or authenticated routes:

```bash
uv run --with playwright python skills/screenshots/scripts/capture_dashboard.py \
  --base-url http://localhost:5173 \
  --authenticate \
  --route speaker-enrollment=/speaker-enrollment \
  --route system=/system
```

For public pages, omit `--authenticate`. Use `--backend-url`, `--env-file`, and `--output-dir` when the deployment differs. It rejects login redirects and blank pages rather than saving misleading screenshots.

For a same-origin HTTPS proxy such as a LAN deployment behind Caddy, point both URLs at the proxy and allow its development certificate (substitute your own host — see your local agent config for the deployment address):

```bash
uv run --with playwright python skills/screenshots/scripts/capture_dashboard.py \
  --base-url https://<deployment-host> \
  --backend-url https://<deployment-host> \
  --ignore-https-errors \
  --authenticate \
  --route recording=/recordings/<recording-id>
```

## Expo screens

The file-based screens are `app/app/index.tsx`, `app/app/diagnostics.tsx`, and `app/app/settings.tsx`. Capture them with Expo’s supported target (`npm run web`, simulator, or device); Playwright can drive the web target, but native-only Bluetooth, audio, share, and permission states must be supplied with fixtures or captured manually on a simulator/device. Include both light and dark themes when visual coverage is the goal.

## Review checklist

- Every route in `App.tsx` and every Expo screen has a result.
- Dynamic routes use valid fixture data.
- Authenticated and unauthenticated states are intentional.
- Screenshots use the same viewport, scale, and theme.
- Full-project reviews cover the route × theme × viewport matrix and safe interaction states.
- Every manifest file exists, totals agree, and no stale or generic retry images remain.
- Phone results report horizontal overflow and include a clipped viewport capture when needed.
- Console, page-error, and request-failure diagnostics are isolated per result.
- Representative desktop, dark, phone, dynamic-detail, and interaction captures are visually inspected.
- No secrets, tokens, or personal data appear in committed images.
- Output artifacts are stored outside source directories and are ignored unless explicitly requested for commit.
