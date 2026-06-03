# Chronicle Vault Sync (macOS)

A menu bar app that keeps your Chronicle Obsidian vault
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

## Prerequisites (Mac)

```bash
brew install syncthing   # the sync engine
# uv (if you don't have it): curl -LsSf https://astral.sh/uv/install.sh | sh
```

You also need the Chronicle **server** side running with vault sync enabled — see
[Server setup](#server-setup-once) below.

## Setup (Mac)

```bash
cd extras/vault-sync
cp .env.template .env
#   edit .env: set AUTH_PASSWORD (and AUTH_USERNAME / BACKEND_URL if needed)

./start.sh                 # run in the foreground (a ◈ icon appears in the menu bar)
```

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

## Server setup (once)

On the machine running the advanced backend:

1. Add a strong key and (optionally) your Tailscale sync address to
   `backends/advanced/.env`:
   ```bash
   VAULT_SYNC_API_KEY=<any long random string>
   VAULT_SYNC_ADDRESS=tcp://<your-host>.ts.net:22000   # optional; aids direct connect
   ```
2. Start the Syncthing service (it's behind a compose profile):
   ```bash
   cd backends/advanced
   docker compose --profile vault-sync up -d vault-syncthing
   ```
   Make sure port **22000** is reachable from your Mac (it is over Tailscale).

The backend's `/api/vault-sync` broker configures the server Syncthing for you when the
Mac pairs — no manual Syncthing setup on the server either.

## Notes

- **Conflicts**: Syncthing keeps both sides; simultaneous edits to the same note from
  the server (AI) and Obsidian produce a `*.sync-conflict-*.md` file rather than losing
  data. In practice the AI mostly creates/appends and you mostly curate, so this is rare.
- **Multiple Macs**: pair each one; they all share the same server folder.
- The local Syncthing uses its own home dir and GUI port (`8385` by default), so it
  won't interfere with any Syncthing you already run.
