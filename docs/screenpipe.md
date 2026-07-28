# ScreenPipe capture nodes

ScreenPipe is Chronicle's local, cross-platform screen/activity capture interface. It
owns the high-volume frame, OCR, accessibility, and optional audio store on each
computer. Chronicle does not mirror that entire store: the companion collector sends
compact, event-driven observations and can fulfill bounded snapshot/OCR requests for
the timeline and memory curator.

## Component boundaries

- `screenpipe record` captures and retains source data locally.
- `extras/screenpipe-collector/` pairs the node with Chronicle, maintains the local
  observation lifecycle, checkpoints progress, and answers bounded media requests.
- Chronicle's backend stores timeline activity and only the media selected for a
  durable Chronicle view or memory.
- `extras/chronicle-tray/` is the single Chronicle desktop entry point, on every
  platform. `app.py` is one `QSystemTrayIcon` that renders in the Linux system tray
  and the macOS menu bar alike; the menu is assembled from `sections/`. It imports
  the vault-sync core (`extras/vault-sync/chronicle_vault_sync/core.py`) in place —
  that package is now a library plus its legacy pre-tray service management, not a
  desktop app of its own.
- ScreenPipe's own desktop UI is an optional, on-demand local timeline viewer. It is
  not the recorder and should not autostart or launch a second recording process.

Do not scan or upload the complete frame stream, and do not copy ScreenPipe's SQLite
database into Chronicle as a second source of truth.

## Services on a node

Four user units exist on a Linux node and they are easy to confuse, because two of
them have "screenpipe" in the name and neither is the one Chronicle ships:

| Unit | What it is | Owned by |
| --- | --- | --- |
| `screenpipe.service` | the recorder — `screenpipe record`, writes `~/.screenpipe/db.sqlite` | upstream ScreenPipe binary; unit file written by Chronicle |
| `chronicle-screenpipe.service` | the collector — reads that DB, forwards observations | `extras/screenpipe-collector/` |
| `chronicle-tray.service` | desktop tray; starts/stops the two above | `extras/chronicle-tray/` |
| `app-screenpipe*@autostart.service` | ScreenPipe's own desktop UI | upstream; **should stay disabled** (see above) |

**Chronicle does not install ScreenPipe.** `init.py` resolves the recorder with
`shutil.which("screenpipe")` and exits with instructions if it is absent; it only
writes the unit around whatever is already on `PATH`. So the recording binary is
whatever was installed by hand — typically the npm `@screenpipe/cli-linux-x64`
package symlinked from `~/.local/bin/screenpipe`, which is **not** a local build.
Check with `readlink -f "$(command -v screenpipe)"` and `screenpipe --version`
before assuming a source change is live.

To run a locally built recorder, repoint that symlink at a copy outside the build
tree; the wizard re-resolves through `which`, and the tray rewrites only the flags
in `ExecStart` (preserving `argv[0]`), so both survive the change.

Reading failures:

- A collector stopped on purpose looks like a crash. Its shutdown handler raises
  `KeyboardInterrupt`, so `systemctl stop` leaves a Python traceback and
  `status=130/n/a` in the journal. A traceback ending in `_shutdown_signal` is a
  clean stop, not a fault.
- `Restart=on-failure` does not resurrect a unit that was explicitly stopped, so a
  collector can stay down for hours while the recorder keeps filling the local DB.
  The two fail independently; check both.

### Focused-window identity on KDE Wayland — fixed 2026-07-26

Frames whose window exposed no accessibility tree used to be stored with `app_name`
and `window_name` NULL. Identity fell back to the AT-SPI tree, so a window with no
tree lost its *name* as well as its text, and since the collector keys context on
application and window title those frames identified nothing.

`get_active_window_info_fresh()` tries Hyprland → Sway → **KWin** → X11. The KWin
step did not exist, and the other three cannot work here: the Hyprland and Sway IPC
sockets are absent, and xdotool sees only XWayland clients.

The fix inverts the lookup, because KWin cannot be *asked* for the focused window —
`getWindowInfo` needs a UUID and reports no active field, `queryWindowInfo` is
interactive, and `plasma_window_management`/`foreign_toplevel` are not advertised to
unprivileged clients. So the recorder serves a D-Bus interface and loads a script
into KWin, and KWin **pushes** focus changes in. Pushing also removes polling and any
staleness window. Verify it live:

```bash
busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting \
  isScriptLoaded s "screenpipe-active-window"          # -> b true
busctl --user introspect com.screenpipe.KWinWatcher /com/screenpipe/KWinWatcher
```

Measured on `text_source='ocr'` frames, the only population affected — NULL `app_name`
went **8,053/8,794 (91%) → 0/2,474 (0%)**, holding at 0% since, including 910
fullscreen Age of Empires IV frames that all resolve a name.

Two cautions for whoever revisits this:

- **Do not "fix" it with xdotool.** On a Wayland-focused window `xdotool getwindowname`
  on the XWayland stub *exits 0 with empty output*. Linux prefers this lightweight
  source over the accessibility tree, so the synthesised `"Unknown"` overwrites correct
  AT-SPI names on every frame. `x11_window_result()` now returns `None` when a lookup
  produced neither a title nor a pid, but the real point is not to install xdotool.
- **The old "fullscreen games above all" framing was too narrow.** Thumbnails of
  pre-fix NULL frames show an ordinary YouTube tab and a Claude Desktop + terminal
  desktop. Any window whose AT-SPI query returned no nodes was affected.

To confirm the fix is live, plot the NULL rate **by hour**
(`GROUP BY substr(timestamp,1,13)`). A sample of recent frames proves nothing: after a
restart they are overwhelmingly `text_source=accessibility`, which carried app names
before the fix too.

Ships on `untracked/screenpipe` branch `chronicle`.

## Observation lifecycle

The companion polls the local ScreenPipe database on its short polling loop, but polling
does not cause a remote upload. It identifies context by application, window title, and
browser URL and sends only these lifecycle events:

- A stable context opens after 10 seconds. The first 10 seconds remain buffered locally.
- Clicks, typing pauses, scroll stops, key presses, clipboard events, visual changes, and
  manual captures make a short context meaningful immediately. Switching to a music
  player, changing playback, and switching back is therefore retained even when it takes
  less than 10 seconds.
- Passive task-switcher, focus, notification, idle, blank, locked, and DRM-paused flashes
  can be folded into the surrounding observation.
- Material OCR/accessibility changes append a sample no more often than every two minutes
  inside the same unchanged observation. App/window switches are not subject to that
  cooldown.
- Accessibility and hybrid text are preferred for observation text and app identity.
  Contextless OCR remains available for visual-only applications, but does not override
  structured text or create a false app/window switch while structured context is active.
- An active unchanged observation receives a lightweight liveness sample after 15
  minutes. A six-hour editor session is still one observation with incremental samples.
- Meaningful switches, capture shutdown, and service shutdown close the observation.

Open state and unsent samples are stored atomically in
`~/.local/state/chronicle-screenpipe/observations.json`. Backend lifecycle upserts and
sample fingerprints make retries idempotent.

Each observation carries at most three ranked local frame pointers. Chronicle normally
requests zero or one 640px preview; the curator may request one different preview. A
selected ScreenPipe image is then fetched at a bounded 1280px size. ScreenPipe remains
the high-resolution source and Chronicle never uploads a frame sequence.

## Screen context and the `chronicle` fork branch

A completed conversation opens a bounded OCR job over its interval, and the collector
answers with the frames in that window. Consecutive frames of an unchanged window
repeat nearly all of their text, so that answer is mostly redundant: measured across
one workstation's conversation windows, 35.7 MB of OCR carried 10.9 MB of distinct
screens.

The reduction belongs where the data is. `untracked/screenpipe` branch `chronicle`
(= `custom/linux-timeline-stability` plus the additions below) carries:

- **`GET /search?dedupe=0.85`** collapses consecutive near-duplicates server-side.
  Measured on one day: 500 rows scanned returned 245 rows and 5.42 MB → 0.82 MB.
  `limit` bounds rows *scanned*, so a deduplicated page is short of the limit while
  data remains — page on `len(data) + deduped`, which the collector does. A recorder
  without the parameter ignores it and reports no `deduped`, so this degrades to the
  old behaviour and Chronicle's own filter still runs.
- **`GET /app-runs`** serves the timeline's segmented view instead of making every
  consumer re-derive it. Runs are **display-grade** — a run says these consecutive
  frames carried this app name, not that an activity began or ended.
- **KWin focused-window tracking**, which is what makes the other two worth having.

### What reliable identity unlocks

Everything that separates one context from another keys on `app_name` + window title:
the collector's observation state machine, `/app-runs`, and the screen-context filter
below. While 91% of tree-less frames were anonymous, none of it could work — those
frames either collapsed into one contextless bucket or borrowed the neighbouring
window's label.

The timeline UI papers over this by copying the previous app name onto unnamed frames
(see `carry_forward` above). The effect of the fix is visible in whether that crutch
still does anything:

| day | `carry_forward=true` | `carry_forward=false` | unnamed frames |
| --- | --- | --- | --- |
| 2026-07-26 (pre-fix) | 54 runs | 212 runs | 2,728 |
| 2026-07-27 (post-fix) | **152 runs** | **152 runs** | **0** |

Before, the choice was between 54 runs carrying fabricated labels and 212 shattered
ones. After, the crutch is a no-op and the two agree exactly — 152 runs that are all
real. Boundaries from app identity are now worth building on; boundaries inferred from
screen *content* still are not (see the segmentation note above).

Only frames whose text came from the accessibility tree are ever compared. `ocr` is
the fallback for windows exposing no tree, where the text is a few HUD fragments that
read alike whether or not the moment is the same. On this KDE Wayland box that split
is absolute: 100% of `accessibility`/`hybrid` frames carry a SimHash and an app name,
and 0% of `ocr`-only frames carry either. Those frames are ~45% of frames but ~4% of
bytes, so collapsing them buys nothing and risks discarding the only record that a
fullscreen session happened.

Beware `app_name` carry-forward: the timeline UI copies the previous app name onto
frames that recorded none, so a fullscreen session is attributed to whatever was
focused before it. On one measured day that relabelled 2,728 of 6,452 frames, turning
212 raw stretches into a tidy-looking 54. `/app-runs` keeps the default but reports
`unnamed_frames` per run, and `carry_forward=false` turns it off.

Chronicle keeps its own filter in `services/device_context.py` as a fallback, because
`init.py` resolves the recorder with `which()` and usually finds the npm build rather
than this fork.

The backend correlates observations with overlapping input/output audio conversations
and nearby Immich candidates. A separate Codex curation pass may discard routine context,
link a duplicate, append a Daily note, update a durable topic/project/event/media note, or
promote a content-addressed image into `_media/`. System-output dialogue is always treated
as media content rather than personal speech. If the visual Codex executor is unavailable,
curation remains pending.

## Capture mode

Microphone and system-output capture are separate sources. A capture node may record
neither, system output only, microphone only, or both. Use
`--audio-transcription-engine disabled` in every enabled mode because Chronicle owns
speech-to-text. ScreenPipe's `--use-system-default-audio true` follows and enrolls both
the default input and output; system-only or microphone-only modes therefore require
an explicit `--audio-device "... (output)"` or `--audio-device "... (input)"` with
`--use-system-default-audio false`. The collector preserves the source direction when
it sends audio to Chronicle.

Local capture and Chronicle forwarding are independent policies. The collector's
`forward_audio` setting accepts `none`, `output`, `input`, or `both`; excluded chunks
remain in ScreenPipe's local store and are checkpointed without upload. Chronicle also
processes input and output as separate sessions so microphone and system audio are not
mixed together before transcription.

The setup wizard's capture-node option delegates to
`extras/screenpipe-collector/init.py`. Pairing and standalone commands are documented
in the [companion README](../extras/screenpipe-collector/README.md).

## Desktop controls and logs

On Linux the Chronicle tray controls `screenpipe.service` and
`chronicle-screenpipe.service`, shows local frame/storage statistics, and offers timed
pauses. **Capture** holds master switches for audio and video; **Capture settings…**
opens a dialog for the per-source choice of what is recorded locally and what is sent
to Chronicle (audio can only be sent from a recorded source; recorded screen frames are
always sent). Both Linux and macOS expose **View Logs** for the
current Chronicle desktop process. System-service history remains in the native
service manager:

```bash
systemctl --user status screenpipe.service chronicle-screenpipe.service chronicle-desktop.service
journalctl --user -u screenpipe.service -u chronicle-screenpipe.service -u chronicle-desktop.service
```

On macOS the same services are launchd agents rather than systemd units, so use
`launchctl print gui/$(id -u)/<label>` and the logs under `~/Library/Logs/Chronicle/`
(labels are in `clients.py`). `extras/vault-sync/start.sh logs` only covers a legacy
pre-tray install.

The Linux ScreenPipe UI launcher may set rendering/onboarding environment required by
the locally installed build, but its desktop autostart entry should remain disabled.
Open that UI manually only when the full local ScreenPipe timeline is needed; the
recorder and Chronicle collector remain independently managed background services.

## Implementation map

- `extras/screenpipe-collector/`: collector CLI, observation state machine, checkpoints, and service installation
- `backends/advanced/src/advanced_omi_backend/routers/modules/device_input_routes.py`: capture-node ingestion API
- `backends/advanced/src/advanced_omi_backend/services/observation_curation.py`: sparse preview and vault curation
- `extras/vault-sync/vault_core.py` and `syncthing_manager.py`: vault sync core and Syncthing pairing/control
- `extras/chronicle-tray/chronicle_tray/sections/vault.py`: unified tray UI adapter (imports the vault-sync core in place)
