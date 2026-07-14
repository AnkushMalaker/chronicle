# Running Chronicle with Podman

Chronicle's service lifecycle (`services.py`, `status.py`, the service-manager
agent) works with either **Docker** (default) or **Podman**. This is useful where
Docker Desktop is unavailable (e.g. locked-down work machines) or when you prefer a
single rootless engine across machines.

Choosing the engine does **not** require editing any compose files — the repo's
existing compose definitions (profiles, healthchecks, `host.docker.internal`, and
NVIDIA `deploy.resources` GPU blocks) run unmodified under Podman.

## Selecting the engine

Precedence: environment variable → `config/config.yml` → `docker` default.

```yaml
# config/config.yml
container_engine: podman        # "docker" (default) or "podman"
# compose_cmd: podman-compose   # optional; default derives from container_engine
```

Or per-shell:

```bash
export CONTAINER_ENGINE=podman          # used for `network`/`inspect`/`ps`
export COMPOSE_CMD="podman-compose"     # used for up/down/build/restart
```

`config.yml` is the recommended home because the **service-manager** and
**discovery** agents run as systemd *user* services and do not inherit your shell
environment.

### Why `podman-compose` (not `docker compose` against the podman socket)

On Ubuntu 24.04 (Podman 4.9), `docker compose` talking to Podman's Docker-compat
socket does **not** perform CDI GPU injection — GPU services start without a GPU.
`podman-compose` drives the `podman` CLI directly, where CDI works. So for podman,
the compose front-end is `podman-compose`. The only consequence is that
`podman-compose`'s `ps` output is not docker-compatible; Chronicle handles this
internally by querying `podman ps` scoped to the compose project label.

## Host setup (rootless)

1. **Install the engine + compose + (GPU) toolkit:**
   ```bash
   sudo apt install -y podman
   uv tool install podman-compose            # or: sudo apt install podman-compose
   # GPU hosts only:
   sudo apt install -y nvidia-container-toolkit
   ```

2. **GPU hosts — generate the CDI spec** (works in WSL2 with the NVIDIA WSL driver):
   ```bash
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   nvidia-ctk cdi list          # expect nvidia.com/gpu=all
   # quick check:
   podman run --rm --device nvidia.com/gpu=all \
     docker.io/nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi
   ```

3. **Clear any Docker Desktop credential helper** (otherwise image pulls fail
   invoking `docker-credential-desktop`):
   ```bash
   # if ~/.docker/config.json contains "credsStore": "desktop..."
   cp ~/.docker/config.json ~/.docker/config.json.bak
   echo '{}' > ~/.docker/config.json
   ```

4. **Select podman** in `config/config.yml` (`container_engine: podman`).

5. **Start services as usual:** `./start.sh`, `./status.sh`, `./stop.sh` — they all
   route through the selected engine.

## Migrating an existing Docker install

- **Data ownership.** Docker creates bind-mounted data (`backends/advanced/data/...`)
  owned by `root` (root-running containers) or by a service UID like `999`
  (mongo/redis drop privileges at runtime). Rootless Podman remaps container UIDs
  through your subuid range, so it can't write either until re-owned. `podman
  unshare chown` alone fails on the root-owned dirs because real root isn't mapped
  into your namespace — so it's two steps:
  ```bash
  # 1) real root pulls everything back to your user (fixes root-owned dirs;
  #    container-root maps to you, so root-running services are then correct)
  sudo chown -R "$(id -un):$(id -gn)" backends/advanced/data
  # 2) remap services that run as a non-root UID inside (mongo + redis = 999),
  #    set from inside the podman user namespace
  podman unshare chown -R 999:999 backends/advanced/data/mongo_data
  podman unshare chown -R 999:999 backends/advanced/data/redis_data
  ```
  General rule for any straggler that can't write: `podman unshare chown -R
  <container_uid>:<container_uid> <dir>`.
- **Free the ports.** Stop/decommission Docker Desktop (or `docker compose down`
  the old stack) so Podman can bind 6379/27017/8000/etc.
- **Docker Desktop PATH shims.** `/usr/bin/docker` and `/usr/bin/docker-compose` are
  often Docker Desktop symlinks that go stale when it's removed. They don't affect
  Podman, but a stale `docker-compose` shim is why a self-contained tool
  (`podman-compose`) is used rather than borrowing Docker Desktop's binary.

## Caveats

- **Privileged ports (Caddy).** Rootless `ip_unprivileged_port_start` is `1024`, so
  Caddy's `443` binds but its `80` HTTP→HTTPS redirect does not. Only relevant with
  the `https`/Caddy profile. If you need it:
  ```bash
  sudo sysctl net.ipv4.ip_unprivileged_port_start=80    # persist in /etc/sysctl.d
  ```
- **inotify watch exhaustion (WebUI 502 / Vite crash loop).** Containers share the
  host's `fs.inotify.max_user_watches` pool (default 65536). A host-side IDE file
  watcher (Cursor/VSCode server) recursively watching this large repo can consume
  nearly the entire pool, starving the webui container's Vite dev server → it
  crash-loops with `ENOSPC` on `watch` and Caddy returns 502. (Instances are not the
  issue — those stay low.) Two fixes, complementary:
  ```bash
  # immediate: raise the watch limit (VSCode/Cursor docs recommend this on big repos)
  echo 'fs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/99-inotify.conf && sudo sysctl --system
  ```
  Also add `files.watcherExclude` in the IDE for heavy paths (`**/node_modules`,
  `**/.venv`, `**/data/**`, `**/model_cache/**`, `golden_data`, `experiments`) so the
  IDE isn't watching tens of thousands of model/data files.
- **Reboot persistence.** Docker's daemon restarts `restart: unless-stopped`
  containers on boot. Rootless Podman is daemonless — nothing re-applies `restart:`
  policies after a reboot (the policy still covers crash-restart while the box is
  up, just not the reboot gap). Instead of Podman's own `podman-restart.service`
  (which only matches `restart-policy=always`, *not* `unless-stopped`), Chronicle
  ships a **`chronicle-stack`** systemd *user* oneshot that runs `services.py start
  --all` on boot — the same path as `./start.sh`, respecting the `config.yml`
  enabled set. It installs (with linger) alongside the node agent via the wizard's
  "Auto-start on boot" prompt or `services.py manager install`, and is ordered
  `After=chronicle-service-manager.service`. So no compose-file edits or
  `podman-restart.service` are needed. Manage/inspect it with `systemctl --user
  status chronicle-stack` and `journalctl --user -u chronicle-stack`.
- **Implicit `default` network.** docker-compose silently creates a `<project>_default`
  network for services that declare no `networks:`. podman-compose instead errors
  `missing networks: default` once any *other* network (e.g. external
  `chronicle-network`) is present. Fix: declare `default: {}` in that compose file's
  top-level `networks:` (a no-op under docker). Only `extras/langfuse` needed this;
  all compose files now parse under both engines.
- **Docker Desktop auto-start.** On Windows, Docker Desktop relaunches on login and
  its `restart: unless-stopped` containers reclaim host ports (27017/6379/…) via the
  WSL relay, blocking Podman. Uninstall it (or disable login-start + `compose down`
  the old stack) — note its `/usr/bin/docker{,-compose}` symlinks then dangle
  harmlessly.

## Toward seamless setup (TODO)

The rootless-podman host prerequisites above are currently manual. To make podman a
one-command choice for the user, the wizard / `services.py` should automate them when
`container_engine: podman` is selected. Each is independently scriptable:

| Manual step today | Who should automate it | Notes |
|---|---|---|
| `unqualified-search-registries=["docker.io"]` + `short-name-mode="permissive"` | wizard (write `~/.config/containers/registries.conf` if absent) | idempotent; required for builds to resolve short names |
| `nvidia-ctk cdi generate` (GPU hosts) | wizard (already detects CUDA via `setup_utils.detect_cuda_version`) | needs sudo; prompt + run |
| `sysctl net.ipv4.ip_unprivileged_port_start=80` | `services.py` preflight when the `https`/Caddy profile is active | needs sudo; only if Caddy enabled. Could also drop Caddy's `:80` listener for rootless |
| Data-dir ownership remap (`sudo chown` + `podman unshare chown 999`) | a `services.py migrate-podman` helper (one-shot) | only when migrating an existing docker install |
| Clear Docker Desktop `credsStore` | wizard (detect `desktop` credsStore, offer to neutralize) | harmless on fresh hosts |

Until automated, `docs/podman.md` (this file) is the checklist. The **engine
abstraction itself** (`container_engine`/`compose_cmd`) is already seamless — only the
host prerequisites need wiring into setup.
