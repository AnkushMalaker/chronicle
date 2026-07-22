# ScreenPipe capture nodes

ScreenPipe is Chronicle's local, cross-platform screen/activity capture interface. It
owns the high-volume frame, OCR, accessibility, and optional audio store on each
computer. Chronicle does not mirror that entire store: the companion collector sends
compact application/window transitions and can fulfill bounded snapshot/OCR requests
for the timeline.

## Component boundaries

- `screenpipe record` captures and retains source data locally.
- `extras/screenpipe-collector/` pairs the node with Chronicle, uploads activity
  metadata, checkpoints progress, and answers bounded media requests.
- Chronicle's backend stores timeline activity and only the media selected for a
  durable Chronicle view or memory.
- `extras/vault-sync/` is the single Chronicle desktop entry point. `main.py` selects
  the macOS menu bar or Linux system tray adapter; `desktop_core.py` contains their
  shared state, logging, and vault synchronization.
- ScreenPipe's own desktop UI is an optional, on-demand local timeline viewer. It is
  not the recorder and should not autostart or launch a second recording process.

Do not scan or upload the complete frame stream, and do not copy ScreenPipe's SQLite
database into Chronicle as a second source of truth.

## Capture mode

Microphone and system-output capture are separate sources. A capture node may record
neither, system output only, microphone only, or both. Use
`--audio-transcription-engine disabled` in every enabled mode because Chronicle owns
speech-to-text. ScreenPipe's `--use-system-default-audio true` follows and enrolls both
the default input and output; system-only or microphone-only modes therefore require
an explicit `--audio-device "... (output)"` or `--audio-device "... (input)"` with
`--use-system-default-audio false`. The collector preserves the source direction when
it sends audio to Chronicle.

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

- `extras/screenpipe-collector/`: collector CLI, local client, checkpoints, and service installation
- `backends/advanced/src/routers/device_activity.py`: capture-node ingestion API
- `backends/advanced/src/services/device_activity_service.py`: activity persistence and media requests
- `extras/vault-sync/main.py`: cross-platform desktop entry point
- `extras/vault-sync/desktop_core.py`: shared desktop state, logs, and vault sync
- `extras/vault-sync/menu_linux.py` and `menu_vault.py`: platform-specific UI adapters
