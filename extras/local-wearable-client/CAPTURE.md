# Screen + Accessibility Capture

The menu bar app can capture your screen (1 fps, one JPEG per display) and read
the focused window's app/title via the macOS Accessibility API. Implementation
lives in `screen_capture.py`; it's pure PyObjC (no Swift).

## What gets captured

| Data | Source | macOS permission |
|------|--------|------------------|
| Screenshot pixels (per display) | ScreenCaptureKit (`SCScreenshotManager`), falls back to `CGDisplayCreateImage` on macOS < 14 | **Screen Recording** |
| Frontmost app name / bundle id | `NSWorkspace.frontmostApplication()` | none |
| Focused **window title** + focused element **role** | Accessibility API (`AXUIElement*`) | **Accessibility** |

Frames are written to `~/ChronicleCaptures/<date>/<HH-MM-SS_mmm>_<i>.jpg`
(`<i>` = display index, `_0` is the main display). Override the location with
`CAPTURE_DIR`.

The Accessibility read is deliberately minimal — only the window title and the
focused element's *role* (e.g. `AXTextArea`), never text-field contents or the
full UI tree.

## Permissions (TCC)

Screen Recording and Accessibility are **independent** grants:

- Missing **Screen Recording** → screenshots are wallpaper-only (other apps'
  windows are blanked), not an error.
- Missing **Accessibility** → `window`/`role` come back empty; the app name
  still resolves.

TCC attaches a grant to the **host binary**. Running `uv run python …` from a
terminal attaches the grant to that terminal/interpreter (flaky, and you'd be
granting e.g. Cursor broad rights). The fix is the `.app` bundle below, so
grants attach to the Chronicle app itself.

> Screen Recording only takes effect after the granted app is **fully
> relaunched**.

## Quick test (no bundle)

```bash
uv run python screen_capture.py --seconds 5      # writes frames, logs focused window
uv run python screen_capture.py --no-write       # log AX info only
```

## Building the .app (stable permissions)

```bash
./build_app.sh                       # ad-hoc signed (grants reset on each rebuild)
CODESIGN_ID="Chronicle Dev" ./build_app.sh   # self-signed cert -> grants persist
open "dist/Chronicle Wearable.app"
```

To make grants survive rebuilds, create a self-signed **Code Signing**
certificate (Keychain Access → Certificate Assistant → Create a Certificate →
Self Signed Root, type "Code Signing") and pass its name via `CODESIGN_ID`.

After first launch: approve the Screen Recording prompt and **re-open the app**;
enable the app under System Settings → Privacy & Security → **Accessibility**
(no prompt is shown for AX — you toggle it manually).

## Why the app is not sandboxed

`setup_app.py` does not request the App Sandbox entitlement. A sandboxed app
**cannot** use the Accessibility API to read other apps' windows — that's
incompatible with the sandbox. Screenshot + focused-window reads require an
unsandboxed app (this is the same reason Omi's own macOS app is unsandboxed).
The app is still fully under TCC control — each capability needs its explicit
user grant.
