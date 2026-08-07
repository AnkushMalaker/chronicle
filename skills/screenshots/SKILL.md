---
name: screenshots
description: Captures consistent screenshots of every Chronicle web or Expo screen with Playwright, including authenticated routes and dynamic pages. Use when documenting UI, reviewing visual regressions, or asked to screenshot all pages.
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

Use `uv run --with playwright` for Python browser automation. If Chromium is not already available, install it ephemerally with `uvx --from playwright playwright install chromium`; do not add Playwright to `package.json` or a Python project environment. Use the exact dashboard host allowed by Chronicle CORS—normally `http://localhost:5173`, not `127.0.0.1`.

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
2. Read `backends/advanced/webui/src/App.tsx` and build the route inventory from its `<Route>` entries. At present the inventory includes `/login`, `/`, `/live-record`, `/chat`, `/recordings`, `/recordings/:id`, `/timeline`, `/timeline/:episodeId`, `/memory-ledger`, `/users`, `/system`, `/system-errors`, `/settings`, `/upload`, `/queue`, `/plugins`, `/finetuning`, `/network`, `/data-audit`, `/speaker-enrollment`, and `/wakeword-lab`. `/conversations` and `/conversations/:id` still resolve, but only as redirects to their `/recordings` equivalents.
3. Use a real recording ID for `/recordings/:id` and a real episode ID for `/timeline/:episodeId`; discover them through the UI or an authenticated API request. Never invent an ID and call that page complete.
4. Vite's HMR socket keeps the page from ever reaching `networkidle`. Wait on `domcontentloaded` plus the page's own content, not on network quiet.
5. Capture `/login` unauthenticated. Log in through the UI, then capture every protected route with the same viewport, theme, and browser state.
6. Wait for the page heading/content and loading indicators to settle. Use `full_page=True`, and save predictable names such as `01-login.png`, `02-recordings.png`, and `03-recording-detail-<id>.png`.
7. Record failures separately. A redirect to login, error boundary, blank page, missing fixture, or unresolved loading state is a failed capture—not a screenshot of the requested page.

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
- No secrets, tokens, or personal data appear in committed images.
- Output artifacts are stored outside source directories and are ignored unless explicitly requested for commit.
