# Chronicle Edge Deployment

Deploy Chronicle services on remote machines (RPi, GPU VMs) with one command. Services auto-discover each other via Tailscale.

## Prerequisites

On the remote machine:
- Docker (with `docker compose`)
- Tailscale (connected to your Tailnet)
- `uv` (Python package manager)
- `git`

## Deploy a Service

```bash
curl -sSL https://raw.githubusercontent.com/SimpleOpenSoftware/chronicle/main/edge/install.sh \
  | bash -s -- <service-name>
```

### Available Services

| Name | Description |
|------|-------------|
| `speaker-recognition` | Speaker identification (pyannote) |
| `asr-services` | Offline speech-to-text (Parakeet/NeMo) |
| `tts` | Text-to-speech synthesis |
| `llm-services` | Local LLM via llama.cpp |
| `havpe-relay` | ESP32 audio bridge |

### Examples

```bash
# Deploy speaker recognition on a GPU machine
curl -sSL https://raw.githubusercontent.com/SimpleOpenSoftware/chronicle/main/edge/install.sh \
  | bash -s -- speaker-recognition

# Deploy from a specific branch
curl -sSL https://raw.githubusercontent.com/SimpleOpenSoftware/chronicle/feat/tailscale-discovery/edge/install.sh \
  | bash -s -- havpe-relay --branch feat/tailscale-discovery

# Custom install directory (default: ~/chronicle)
CHRONICLE_HOME=~/my-services curl -sSL ... | bash -s -- havpe-relay
```

## What Happens

1. Clones the repo to `~/chronicle/` (or `$CHRONICLE_HOME`)
2. Runs the service's interactive config wizard (API keys, credentials, etc.)
3. Starts the service and the **node agent** (`edge/service_manager.py`) — a small
   native host process that advertises the service on your Tailnet *and* survives
   reboot via a systemd user service
4. Service appears on the **Network** page of your Chronicle dashboard, with live
   health

The node agent is the default. Pass **`--advertise-only`** (alias `--sidecar`) to use
the legacy containerized `edge-agent` sidecar instead — advertise-only, no control, no
host process. (`havpe-relay` always uses the sidecar; it isn't node-agent-managed.)

## Manage Edge Services

**Default (node agent):**
```bash
cd ~/chronicle
./status.sh                                   # node + service health
uv run --with-requirements setup-requirements.txt python services.py stop <service>
```

**Advertise-only sidecar (`--advertise-only`):**
```bash
cd ~/chronicle/extras/<service-dir>
docker compose --profile edge ps      # status
docker compose --profile edge logs -f # logs
docker compose --profile edge down    # stop
```

## Update a Node

```bash
cd ~/chronicle
uv run --with-requirements setup-requirements.txt python services.py update --check
uv run --with-requirements setup-requirements.txt python services.py update
```

Branch installs pull their branch; release-tag installs move to the latest `v*`
tag. Enabled services are rebuilt and restarted from the new code (rolled back
if one fails to come up). Nodes running the node agent can also be updated
remotely from the hub's WebUI System page (the hub fans out to peer agents over
the Tailnet). See `docs/fleet-updates.md`.

## How It Works

By default the **node agent** runs natively on the edge box: it starts the service via
`services.py` and advertises it on your Tailnet via minidisc (Tailscale already running
on the host). The main Chronicle backend discovers it automatically — no manual IP
configuration needed. Running natively is required because the agent also drives
`docker compose` against the host (host bind-mount paths) and, on Docker Desktop/WSL2, a
container can't bind the Tailscale interface to advertise.

```
RPi / GPU VM                          Main Server
─────────────                         ───────────
Docker: asr-service                   Docker: chronicle-backend
Host:   node agent (native)    ←TS→   GET /api/system/network
        ↓                                    ↓
   advertises on Tailnet              Network page shows node + health
```

With `--advertise-only`, the advertiser is instead a tiny Docker sidecar (`edge-agent`,
`network_mode: host` + `tailscaled.sock`) that rides the service's compose lifecycle —
no host process, but no control and no WSL2 support.
