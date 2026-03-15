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

# Custom install directory (default: ~/.chronicle)
CHRONICLE_HOME=~/my-services curl -sSL ... | bash -s -- havpe-relay
```

## What Happens

1. Clones the repo to `~/.chronicle/` (or `$CHRONICLE_HOME`)
2. Runs the service's interactive config wizard (API keys, credentials, etc.)
3. Starts the service + an edge-agent sidecar container
4. The edge-agent advertises the service on your Tailnet via minidisc
5. Service appears on the **Network** page of your Chronicle dashboard

## Manage Edge Services

```bash
cd ~/.chronicle/extras/<service-dir>

# Status
docker compose --profile edge ps

# Logs
docker compose --profile edge logs -f

# Stop
docker compose --profile edge down

# Restart
docker compose --profile edge up -d
```

## How It Works

The edge-agent is a tiny Docker sidecar that advertises the service on your Tailnet using minidisc. The main Chronicle backend discovers it automatically — no manual IP configuration needed.

```
RPi / GPU VM                          Main Server
─────────────                         ───────────
Docker: speaker-service               Docker: chronicle-backend
Docker: edge-agent (sidecar)   ←TS→   GET /api/system/network
        ↓                                    ↓
   advertises on Tailnet              Network page shows node
```
