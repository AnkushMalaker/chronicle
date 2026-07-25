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
- `extras/vault-sync/` is the single Chronicle desktop entry point. `main.py` selects
  the macOS menu bar or Linux system tray adapter; `desktop_core.py` contains their
  shared state, logging, and vault synchronization.
- ScreenPipe's own desktop UI is an optional, on-demand local timeline viewer. It is
  not the recorder and should not autostart or launch a second recording process.

Do not scan or upload the complete frame stream, and do not copy ScreenPipe's SQLite
database into Chronicle as a second source of truth.

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
`chronicle-screenpipe.service`, shows local frame/storage statistics, toggles capture
modes, and offers timed pauses. Both Linux and macOS expose **View Logs** for the
current Chronicle desktop process. System-service history remains in the native
service manager:

```bash
systemctl --user status screenpipe.service chronicle-screenpipe.service chronicle-desktop.service
journalctl --user -u screenpipe.service -u chronicle-screenpipe.service -u chronicle-desktop.service
```

On macOS, use `extras/vault-sync/start.sh logs` for the installed desktop service.

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
