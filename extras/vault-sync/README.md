# Chronicle Desktop Tray and Vault Sync

A macOS menu bar / Linux system tray app that keeps your Chronicle Obsidian vault
(`data/conversation_docs/{your_user}` on the server) synced to a folder on your Mac, so
you can open it in **Obsidian** with full backlinks, graph view, etc.

Under the hood it runs a private, headless [Syncthing](https://syncthing.net) and pairs
it with the server automatically through the Chronicle backend — you never touch the
Syncthing UI. It's a sibling of the other menu bar apps (`havpe-relay`,
`local-wearable-client`): same `rumps` + `uv` + launchd pattern.

```
Server Syncthing  ◀──── sync protocol :22000 (over Tailscale) ────▶  Mac Syncthing
  (data/conversation_docs/{user})                                    (~/ChronicleVault)
        ▲                                                                   │
        │  pairing brokered by backend  /api/vault-sync                     ▼
        └───────────────  Mac authenticates with its JWT  ───────────  Obsidian
```

## Prerequisites

```bash
brew install syncthing   # the sync engine
# uv (if you don't have it): curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Arch/CachyOS:

```bash
sudo pacman -S obsidian syncthing
```

Both platforms use the same desktop entry point and share their state, logging, and
vault-sync orchestration; only the native menu UI is platform-specific. Both provide
**View Logs** for recent desktop activity. The Linux tray also shows local ScreenPipe frame/audio counts and storage use, and
provides start, stop, and restart controls for `screenpipe.service` and
`chronicle-screenpipe.service`. Its top-level Settings menu can enable or disable audio
and screen capture, while the ScreenPipe menu can pause capture for 5, 15, or 30
minutes, or 1, 2, or 8 hours.

You also need the Chronicle **server** side running with vault sync enabled — see
[Server setup](#server-setup-once) below.

## Setup

```bash
cd extras/vault-sync
cp .env.template .env
#   edit .env: set AUTH_USERNAME (email), AUTH_PASSWORD, and BACKEND_URL

./start.sh                 # run in the foreground (a ◈ icon appears in the menu bar)
```

Vault sync first reads the repository root `.env`, then the optional
`extras/vault-sync/.env` as an override. You can use the root `ADMIN_EMAIL` and
`ADMIN_PASSWORD`, or set `AUTH_USERNAME` and `AUTH_PASSWORD` specifically for the
desktop sync app. It also needs `BACKEND_URL` unless backend discovery works on the
machine. The pairing broker hands back everything else (server device id,
sync address) — you never set `VAULT_SYNC_*` on the Mac; those are server-only.

> **macOS + Tailscale: set `BACKEND_URL` explicitly.** Auto-discovery (minidisc) needs
> the `tailscaled` unix socket at `/var/run/tailscale/tailscaled.sock`, which the **macOS
> GUI / App-Store Tailscale app does not expose** (it runs as a sandboxed network
> extension). So on a Mac, discovery returns nothing and the app would fall back to
> `localhost`. Just point at your server's MagicDNS name — that *is* discovery for a
> fixed server, and needs no socket:
> ```bash
> BACKEND_URL=https://<your-host>.ts.net
> ```
> Confirm with `test -S /var/run/tailscale/tailscaled.sock && echo present || echo absent`
> (absent is normal). The app logs exactly which path it took — see **View Logs**.

On launch it starts Syncthing, authenticates to Chronicle, pairs, and begins syncing
into `~/ChronicleVault` (or `LOCAL_VAULT_DIR`). From the menu:

- **Open in Obsidian** — opens the synced folder as a vault
- **Choose Vault Folder…** — pick a different local folder (re-pairs automatically)
- **Sync Now / Re-pair** — re-run the handshake
- **View Logs** — recent activity

### Run it as a login item (always on)

```bash
./start.sh install      # installs a launchd agent + "Chronicle Vault Sync.app"
./start.sh status
./start.sh logs
./start.sh uninstall
```

On macOS this installs a launchd agent. On Linux it installs
`chronicle-desktop.service` as a systemd user service attached to the graphical
session.

## Server setup (once)

On the machine running the advanced backend:

1. Add a strong key and your Tailscale sync address to `backends/advanced/.env`:
   ```bash
   VAULT_SYNC_API_KEY=<any long random string>
   VAULT_SYNC_ADDRESS=tcp://<your-host>.ts.net:22000   # what the Mac dials; port 22000
   ```
2. Start the Syncthing service (it's behind a compose profile) and (re)create the
   backend so it picks up the new env + the broker route:
   ```bash
   cd backends/advanced
   docker compose --profile vault-sync up -d vault-syncthing
   docker compose up -d --force-recreate chronicle-backend
   ```
   Make sure port **22000** is reachable from your Mac (it is over Tailscale).
3. Verify the broker chain (should return a `server_device_id` + your `sync_address`):
   ```bash
   TOKEN=$(curl -s -X POST -d "username=<email>&password=<pass>" \
     http://localhost:8000/auth/jwt/login | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
   curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/vault-sync/info
   ```

The backend's `/api/vault-sync` broker configures the server Syncthing for you when the
Mac pairs — no manual Syncthing setup on the server either.

> **Use `--force-recreate`, not `restart`.** The backend bind-mounts `discovery.py` as a
> single file; editing it changes the inode and a plain `docker compose restart` fails to
> re-establish the mount (especially on Docker Desktop / WSL). `up -d --force-recreate`
> recreates the container with fresh mounts. A recreate is also what injects newly added
> `.env` values into the container's environment.

## Notes

- **Conflicts**: Syncthing keeps both sides; simultaneous edits to the same note from
  the server (AI) and Obsidian produce a `*.sync-conflict-*.md` file rather than losing
  data. In practice the AI mostly creates/appends and you mostly curate, so this is rare.
- **Multiple Macs**: pair each one; they all share the same server folder.
- The local Syncthing uses its own home dir and GUI port (`8385` by default), so it
  won't interfere with any Syncthing you already run.
