# ScreenPipe capture nodes

ScreenPipe is Chronicle's local, cross-platform screen/activity capture interface. It
owns the high-volume frame, OCR, accessibility, and optional audio store on each
computer. Chronicle does not mirror that entire store: the companion collector sends
compact, event-driven observations and can fulfill bounded snapshot/OCR requests for
the timeline and memory curator.

Continuous ScreenPipe audio is timeline evidence rather than a user-facing
Conversation. Approximately 30-second source files are assembled into bounded compute
spans; Chronicle persists compact 10-second speech/acoustic/coverage arrays and runs
changed-day semantic analysis on a configurable 30-minute cadence. Neither the source
files nor the compute spans impose episode boundaries. See
[Semantic timeline episodes](backend/timeline-episodes.md).

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

Chronicle enforces that ownership rule when its recorder component is installed. It
disables the ScreenPipe desktop app's login autostart (without uninstalling the app),
then starts `screenpipe record` as the single login recorder. The desktop app remains
manually launchable. If it is opened later, the Chronicle tray yields recording to the
app by stopping Chronicle's recorder job; it does not silently restart Chronicle when
the app exits, because that could override an intentional pause.

Recorder status distinguishes `Chronicle recorder active`, `desktop app owns
recording`, and `port 3030 is owned by an unrecognized process`. Start/restart actions
are rejected with that detail while another owner is active. On macOS the status also
warns when the desktop app's readable settings have meeting detection disabled; the
third-party preference is never rewritten automatically.

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

### The frame shortlist, and who chooses from it

Each observation carries up to six local frame pointers, **stratified across its span**:
the observation is divided into equal slices and the best-scoring frame of each slice is
kept. Ranking by score alone does not work here — consecutive frames of an unchanged
window score almost identically, so the top few are usually neighbours. Measured on this
deployment before the change, observations longer than 15 minutes had all their
candidates inside 5.8% of their duration (154s median): a 45-minute session was
represented by one 2.5-minute slice of itself.

Chronicle requests that shortlist as **one** job (`observation_preview_shortlist`,
`payload.frame_ids`), and the collector answers with all of the frames it can still
serve in a single `POST /jobs/{id}/previews`. Frames are best-effort: ScreenPipe prunes
its store, so a missing frame is expected, does not fail the batch, and is recorded in
`metadata.preview_shortlist_missing`.

The curation agent is then shown every fetched frame and picks the one that represents
the session (`selected_frame_id`), or none. That selection — not the scorer's guess —
becomes the observation's `media_data`, and the rest of the shortlist is dropped: it
existed to be judged, and storing every frame costs several times the one that was
picked. ScreenPipe remains the high-resolution source and Chronicle never uploads a
frame sequence.

**A chosen frame is timeline evidence, so it survives a `discard`.** Discarding means
the vault needs no note about the observation, not that the day's visual timeline should
have a blank where it happened — `services/timeline/evidence.py` turns `media_data` into
an evidence item's `image_filename`, which is what an episode's `representative_image`
is drawn from. Only `promote_image`/`retain_image` puts an image *inside* a note, and
that path additionally fetches the frame at a bounded 1280px and content-addresses it
under `_media/`. The two decisions are independent: most observations should yield a
timeline thumbnail and no vault image.

### An episode's picture comes from its own interval

An observation's shortlist is fixed while that observation is open, which makes it the
wrong source for a *timeline episode*: the episode is a longer, semantic span, and it
inherits only whatever frames happened to be shortlisted inside it. Measured here, the
55-minute episode "Rematch multiplayer gaming" (17:34–18:29) cited exactly two
image-bearing observations, both from its first twenty minutes, so its picture was the
game's **main menu** while the in-match frames sat unused on observations at 18:36 and
18:55 — outside its bounds.

ScreenPipe holds every frame, not just the shortlisted ones: `GET /search` reports **516
frames** for one 30-minute stretch of that session. So an episode asks the node to sample
*its own* interval instead. `process_episode_thumbnails` (`services/timeline/thumbnails.py`,
cron `episode_thumbnails`) runs in two phases so a tick never blocks on the node:

1. An episode with no frames gets an `episode_frames` job carrying its interval and a
   count. The collector divides that interval into equal slices and resolves one frame
   per slice with a narrow `limit=1` query — enumerating all 516 to choose six would be
   pure waste — then uploads them through the same `POST /jobs/{id}/previews` batch.
2. When the frames land, a bounded vision pass picks the one that depicts the episode,
   told to prefer activity in progress over menus, launchers, and loading screens. The
   choice becomes `representative_image` and the rest are dropped.

`thumbnail_state` (`""` → `requested` → `chosen`/`unavailable`) makes both terminal
states durable, so an interval the recorder never covered is not re-requested forever.

This is independent of `representative_evidence_id`, which the segmentation agent still
sets from evidence it cites; the dedicated pass exists because that agent can only
nominate frames some observation already fetched.

The request is bounded: a shortlist is asked for at most twice, after which curation
proceeds on text alone. It must be, because a frame ScreenPipe has pruned returns 404
forever. The previous per-frame retry had no cap and re-requested a single dead frame on
every cron tick — 13,113 failed jobs, 821 of them for one frame id — and because
curation refused to proceed without an image, **83% of all observations sat at
`pending` and were never written to the vault at all**. A stalled visual fetch must
degrade to a text memory, never to no memory.

> **Deployment order.** A collector older than this change does not understand
> `frame_ids` and fails the job. That is bounded rather than fatal — two failures and
> the observation curates on text — but a node keeps producing image-less memories
> until its collector is updated.

## Meeting detection and audio session bounds

A meeting signal is the strongest evidence of where a conversation begins and ends,
because it comes from the machine rather than from inference. Without one the backend
falls back to the speech profile (see "Where a recording begins and ends" below), which
is good but not authoritative. The collector supplies real meeting intervals from two
sources, recorder first:

1. **The recorder's own meetings table (macOS / Windows — preferred).** ScreenPipe's
   meeting watcher (CoreAudio process taps / WASAPI sessions) persists meetings —
   with titles — into `db.sqlite`'s `meetings` table. The collector mirrors those
   rows into Chronicle events and, because the rows are persisted, this is
   **retroactive**: a meeting recorded while the collector was down still gets its
   bounds on backfill. The recommended recording flags therefore no longer pass
   `--disable-meeting-detector`; use ScreenPipe's `ignored_meeting_apps` for
   exclusions. Once the recorder has written a meeting recently, it owns detection
   on that node and the collector's own sensor stands down.
2. **The collector's PipeWire tracker (Linux fallback).** Upstream ScreenPipe has no
   Linux sensor, so the collector runs the pattern the bot-free note-takers
   (Granola, ScreenPipe's watcher) converged on: a process holding a *running*
   PipeWire microphone capture stream (`pw-dump`, `media.class =
   Stream/Input/Audio`, `state = running` — browsers cork the stream outside calls)
   is the trigger; classification is allowlist-only (known meeting apps directly, a
   browser attributed through the current observation's `browser_url`, falling back
   to `browser-call`), so always-on capturers — ScreenPipe itself, OBS, dictation
   tools — can never mint meetings. Hysteresis: two consecutive sightings to open
   (pre-join lobbies, mic tests), a 30 s grace before closing at the drop time, and
   a stale-sensor close so a dead `pw-dump` cannot pin a meeting open. Live-only:
   intervals cover only time the collector was running.

Meeting boundaries ship as ordinary observation open/close events
(`metadata.observation_type = "meeting"`, `detection_source = recorder|pipewire`),
so they land on the timeline and join overlapping conversations through the existing
correlation. Each forwarded audio chunk overlapping a meeting interval — state
persists in `~/.local/state/chronicle-screenpipe/meetings.json` — carries a
`meeting_id`, and the backend's session grouping (`device_audio_ingest.py`) splits
on meeting boundaries and tolerates 5-minute silences inside one meeting. A window
carrying a meeting id is never cut by the speech-derived rule below: its bounds came
from a real signal. Disable per node at pairing time with `--no-meeting-detection`.

## Where a recording begins and ends

ScreenPipe records continuously, so the 60-second gap rule almost never fires — the
recorder does not stop just because nothing is happening. For a long time the only
other splitter was a hard 30-minute cap, which cut wherever the clock landed.

Measured over this deployment's corpus of 237 ScreenPipe recordings: 95.9 hours of
stored audio carrying 24.9 hours of speech, 176 recordings sitting exactly at the cap,
and **94 of those with speech running to within 15 seconds of the cut**. More than half
of all capped recordings were severed mid-conversation, with the remainder filed as a
separate recording that began 30 minutes later.

Two independent changes fix the two halves of that, and they compose:

- **The window is bounded for compute; the boundary is chosen from speech.**
  `group_audio_sessions` now accumulates up to a 2-hour window, which is mixed and
  profiled once. `plan_session_cuts` then reads the 10-second speech series and cuts at
  the middle of the longest quiet run near a 30-minute target, widening its search to
  the safety cap before settling. Replayed over the corpus' rebuilt capture windows, a
  fixed 30:00 cut severed speech on **148 of 334 cuts (44%)**; choosing the quietest
  point severs **34 of 308 (11%)**, and none of the remainder are forced by the safety
  cap — they are windows with no long quiet run anywhere in two hours.
- **Silence inside a recording is trimmed, not stored inline.** See
  [memories.md](backend/memories.md) for the vault side; the audio side is
  `utils/audio_trim.py`, invoked after transcription by `maybe_trim_silence`.

Neither change deletes audio. Trimmed stretches move to a soft-deleted remnant
conversation, and every chunk keeps an immutable `captured_at`, so a remnant needs no
"trimmed from here" record — the audio itself says when it happened.

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
and nearby Immich candidates. Observations are then **evidence for timeline episodes**,
not vault content in their own right: what reaches the vault is decided per *day of
episodes*, by the settled-day pass in
[timeline-episodes.md](backend/timeline-episodes.md#the-day-not-the-conversation-is-the-memory-unit).
System-output dialogue is always treated as media content rather than personal speech.

An earlier per-observation Codex curation pass wrote a Daily note per observation. It has
been retired: an observation is a coarse application session, so curating one was the
"an observation is not an event" error — it split a single activity across many notes and
could never see the day around it.

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

### Updating the recorder (prebuilt, no toolchain)

The fork's CI publishes prebuilt recorder CLIs (linux-x86_64 and macos-aarch64)
under the rolling `chronicle-latest` prerelease on `AnkushMalaker/screenpipe` on
every push to its `chronicle` branch, with a `manifest.json` carrying the fork
commit and per-asset sha256s. The tray's **Update recorder…** consumes it:
download → sha256 verify → swap into `~/.local/lib/screenpipe-cli-chronicle/current/`
→ repoint the `screenpipe` symlink → restart the service. The prior build stays
in `previous/`, so **Revert recorder update** is a directory swap. Headless
nodes can run the same flow with
`uv run --project extras/chronicle-tray python -m chronicle_tray.recorder_update check|install|revert`.
`install-local.sh` / `install-cli-local.sh` in the fork remain the build-from-source
path for recorder development. macOS caveat: the CLI is ad-hoc signed, so its
CDHash changes each build and TCC re-prompts for Screen Recording after an
update — same as a local rebuild; only an Apple Developer certificate would
remove that.

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

Installing Chronicle's recorder selects Chronicle as the login owner. To deliberately
restore the ScreenPipe desktop app as the login owner after removing/stopping the
Chronicle recorder, reverse the platform override:

```bash
# macOS — use the Label from the app's plist
launchctl enable "gui/$(id -u)/screenpipe - Development"

# Linux — use the exact generated unit shown by list-unit-files
systemctl --user list-unit-files 'app-screenpipe*@autostart.service'
systemctl --user unmask 'app-screenpipe\x20\x2d\x20Development@autostart.service'
```

## Implementation map

- `extras/screenpipe-collector/`: collector CLI, observation state machine, checkpoints, and service installation
- `extras/screenpipe-collector/chronicle_screenpipe/meeting.py`: PipeWire meeting detection and audio-chunk tagging
- `backends/advanced/src/advanced_omi_backend/routers/modules/device_input_routes.py`: capture-node ingestion API
- `backends/advanced/src/advanced_omi_backend/services/device_audio_ingest.py`: meeting-aware audio sessionization into conversations
- `backends/advanced/src/advanced_omi_backend/services/timeline/memory.py`: settled-day episode memory into the vault
- `extras/vault-sync/vault_core.py` and `syncthing_manager.py`: vault sync core and Syncthing pairing/control
- `extras/chronicle-tray/chronicle_tray/sections/vault.py`: unified tray UI adapter (imports the vault-sync core in place)
