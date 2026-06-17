# Screen + Accessibility Capture

The menu bar app captures your screen (1 fps, one JPEG per display), reads the
focused window's context via the macOS Accessibility API, and writes a
structured metadata sidecar (`events.jsonl`) you can run analytics over.
Implementation lives in `screen_capture.py` (pure PyObjC, no Swift);
`analyze.py` reports app/title/URL time from the sidecar.

## What gets captured

| Data | Source | macOS permission |
|------|--------|------------------|
| Screenshot pixels (per display) | ScreenCaptureKit (`SCScreenshotManager`), falls back to `CGDisplayCreateImage` on macOS < 14 | **Screen Recording** |
| Frontmost app name / bundle id | `NSWorkspace.frontmostApplication()` | none |
| User-idle seconds (since last input) | `CGEventSourceSecondsSinceLastEventType` | none |
| Focused **window title**, element **role/subrole**, **URL** (browsers/doc apps), **selected-text length** | Accessibility API (`AXUIElement*`) | **Accessibility** |
| On-screen text (**OCR**, opt-in) | Apple Vision (`VNRecognizeTextRequest`) | (uses the screenshot) |

Frames are written to `~/ChronicleCaptures/<date>/<HH-MM-SS_mmm>_<i>.jpg`
(`<i>` = display index, `_0` is the main display). Override the location with
`CAPTURE_DIR`.

The Accessibility read is deliberately minimal — window title, element role, the
URL, and the **length** of any selected text (not its content). It never reads
text-field contents or walks the full UI tree.

## Metadata sidecar (`events.jsonl`)

One JSON object per tick is appended to `~/ChronicleCaptures/<date>/events.jsonl`:

```json
{
  "ts": "2026-06-17T10:24:28.551", "epoch": 1750148668.551,
  "app": "Cursor", "bundle_id": "com.todesktop...", "pid": 1234,
  "window_title": "screen_capture.py — friend-lite",
  "url": null, "focused_role": "AXTextArea", "focused_subrole": null,
  "selected_text_len": 0, "idle_seconds": 2.3,
  "displays": [
    {"index": 0, "file": "2026-06-17/10-24-28_551_0.jpg", "w": 2560, "h": 1440, "ocr_file": null},
    {"index": 1, "file": "2026-06-17/10-24-28_551_1.jpg", "w": 3024, "h": 1964, "ocr_file": null}
  ]
}
```

`file`/`ocr_file` are relative to the capture dir. Disable the sidecar with
`--no-events` (standalone) — but then there's nothing to analyze.

## OCR (optional, opt-in)

Apple Vision OCR runs on each frame when enabled, writing recognized text to a
`.txt` sidecar next to each JPEG and referencing it from `ocr_file`. It's
CPU-heavy at 1 fps, so it's **off by default**.

- Standalone: `uv run python screen_capture.py --ocr`
- Under the agent: set `CAPTURE_OCR=1` in `.env`

## Analytics

```bash
uv run python analyze.py                    # today, by app
uv run python analyze.py --days 7           # last 7 days
uv run python analyze.py --date 2026-06-17  # one day  (or --date all)
uv run python analyze.py --by title         # group by window title
uv run python analyze.py --by url           # group by URL
```

It attributes the gap between consecutive ticks to whatever was focused, buckets
inactivity (`idle_seconds >= --idle-threshold`, default 120s) as `(idle)`, and
ignores gaps longer than `--max-gap` (default 5s) so paused/stopped periods
don't invent time. Pure stdlib — you can copy `events.jsonl` off the Mac and run
it anywhere.

## Permissions (TCC)

Screen Recording and Accessibility are **independent** grants:

- Missing **Screen Recording** → screenshots are wallpaper-only (other apps'
  windows are blanked), not an error.
- Missing **Accessibility** → `window`/`role` come back empty; the app name
  still resolves.

TCC attaches a grant to the **responsible process**. How the app is launched
decides what that is:

- **From a terminal** (`uv run python …`): the responsible process is the
  *terminal app* (Terminal/iTerm/Cursor) — so you'd be granting that terminal,
  which is flaky and over-broad.
- **From the launchd agent** (recommended, below): there is no terminal, so the
  responsible process is the agent's own **Python** process. The grant attaches
  to that Python and is independent of any terminal.

> Screen Recording only takes effect after the granted process is **restarted**.

## Quick test (from a terminal)

```bash
uv run python screen_capture.py --seconds 5      # writes frames, logs focused window
uv run python screen_capture.py --no-write       # log AX info only
```

If you see `SCStreamErrorDomain Code=-3801 "The user declined TCCs"` (or
`screen_recording=False` with no prompt), macOS cached a **denial** for the host
process (your terminal) and won't re-prompt. Either add your terminal under
System Settings → Privacy & Security → **Screen Recording** and relaunch it, or
reset and re-run:

```bash
tccutil reset ScreenCapture
uv run python screen_capture.py --seconds 5
```

This terminal-identity flakiness is exactly why the launchd agent is the better
home for the always-on capture — grants attach to the agent's Python, not your
terminal.

## Deployment: the launchd agent

The menu bar app (screen capture toggle included) runs as a launchd user agent.
This is the intended always-on deployment — no separate `.app` to build.

```bash
./start.sh install      # installs the launchd agent + a Spotlight launcher
./start.sh logs         # tail the agent log
./start.sh uninstall    # remove it
```

The agent runs the menu app via `uv run` with the project as the working
directory, so `.env` / `devices.yml` load normally (nothing is frozen).

### Granting capture permissions to the agent

1. `./start.sh install` and let the agent start (the ⊙ menu bar icon appears).
2. Menu → **Grant Capture Permissions**. This triggers the Screen Recording and
   Accessibility prompts *for the agent's Python* (its own TCC identity, not your
   terminal's). Approve both.
3. Restart the agent so Screen Recording takes effect:
   ```bash
   ./start.sh kickstart      # or: launchctl kickstart -k gui/$(id -u)/com.chronicle.wearable-client
   ```
4. Menu → **Screen Capture: Off → On** (or set `CAPTURE_AUTOSTART=1`, below).

If the prompt doesn't appear, the binary still shows up (greyed) in System
Settings → Privacy & Security → Screen Recording / Accessibility after the first
attempt — toggle it on there, then `kickstart`.

### Auto-start capture under the agent

By default capture starts **off** (toggle from the menu). To have it begin
automatically on agent launch, set `CAPTURE_AUTOSTART=1` in `.env` (the installer
copies `.env` into the agent's environment). It only starts once Screen Recording
is actually granted; otherwise it logs a warning and waits for the toggle.

### Caveat: Python version upgrades

The grant is tied to the Python binary `uv` runs. If `uv` upgrades its managed
Python (e.g. 3.12.8 → 3.12.9), the path changes and you'll need to re-grant. Pin
it to avoid surprises:

```bash
echo "3.12.8" > .python-version    # keep uv on a fixed Python -> stable grant
```

## Privacy

Everything stays on disk under `~/ChronicleCaptures` — screenshots, `events.jsonl`,
and any OCR `.txt` files. Nothing is uploaded by this module. URLs and OCR text
*are* recorded when enabled (OCR is opt-in); selected text is stored as a length
only, never its content. Delete a day by removing its `<date>` folder.

## Why no sandbox / no frozen .app

- **No App Sandbox**: a sandboxed process **cannot** use the Accessibility API to
  read other apps' windows. The agent's Python runs unsandboxed (still fully
  under TCC — every capability needs its explicit grant).
- **No py2app/PyInstaller bundle**: `uv` ships a *standalone* Python, which
  py2app can't freeze (it expects a framework build — fails on `zlib.__file__`).
  The launchd agent avoids freezing entirely by running the source directly.
