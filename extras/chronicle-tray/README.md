# Chronicle Tray

The one desktop tray for a Chronicle client machine (macOS menu bar / Linux
system tray). It replaces the separate vault-sync and wearable menu bar apps
with a single icon whose menu shows only what this machine can do:

| Section | What it does | Shown when |
|---------|--------------|------------|
| **Vault Sync** | Syncs your Obsidian vault with the server via a private Syncthing (config in the repository-root `.env`) | `syncthing` binary installed |
| **ScreenPipe** | Local capture stats + start/stop/restart for `screenpipe.service` and the Chronicle collector | `screenpipe` installed or a local DB exists |
| **Pendant** | Scan/connect/stream from BLE wearables (OMI, Neo1, Friend; config in `extras/local-wearable-client/`) | installed with `--pendant` |

Unavailable sections show a disabled hint line with the install command instead
of vanishing.

## Run / install

```bash
cd extras/chronicle-tray
uv run chronicle-tray                    # foreground (default: run)
uv run chronicle-tray install            # login service (launchd / systemd user unit)
uv run chronicle-tray install --pendant  # + BLE wearable streaming deps
uv run chronicle-tray status|restart|logs|uninstall
```

Installing the tray removes the superseded `chronicle-desktop.service` /
`com.chronicle.vault-sync` units (two trays would fight over the same private
Syncthing). Vault sync reads `BACKEND_URL`, `AUTH_USERNAME`, and `AUTH_PASSWORD`
from the repository-root `.env`.

The service unit is defined in the repo-root `clients.py` (shared with
`services.py client` and the node agent), and runs `uv run` from this checkout,
so a node update (`services.py update` or the WebUI update button) restarts the
tray on the new code automatically.

## Section internals

The tray reuses the sibling projects' logic in place rather than duplicating it:
vault sync from `extras/vault-sync` (`vault_core.py`, `syncthing_manager.py`),
pendant BLE from `extras/local-wearable-client` (`ble_manager.py`). Their state
directories and backend pairing flows are reused unchanged.
