# Chronicle Edge Deployment

## Vision

Chronicle's distributed deployment model, inspired by [NullClaw](https://github.com/nullclaw/nullclaw) and [minidisc](https://github.com/mscheidegger/minidisc), enables zero-config edge deployments across a Tailscale network.

The core idea: separate **Chronicle Core** (heavy AI workloads) from **Chronicle Edge** (lightweight audio capture/relay), with automatic service discovery via minidisc eliminating all manual IP/URL configuration.

## Architecture

```
+--------------------+        +-----------------------------+
|  Raspberry Pi      |        |  AI Workstation             |
|  "Chronicle Edge"  |        |  "Chronicle Core"           |
|                    |        |                             |
|  - OMI BLE relay   |  TS +  |  - FastAPI backend          |
|  - Mic capture     | minidisc|  - RQ workers (GPU)        |
|  - Audio forwarding|<------>|  - MongoDB, Redis, Qdrant  |
|  - minidisc client |        |  - LLM inference            |
|  - ~tiny footprint |        |  - minidisc server          |
|                    |        |                             |
+--------------------+        +-----------+--+--------------+
                                          |
                              +-----------v-----------------+
                              |  Phone                      |
                              |  - Chronicle App            |
                              |  - QR code scan to pair     |
                              |  - Tailscale for transport  |
                              +-----------------------------+
```

## Key Components

### Chronicle Core (AI Workstation)
The full backend stack — FastAPI, RQ workers, MongoDB, Redis, Qdrant, LLM inference. Advertises itself on the Tailnet via minidisc. This is where all heavy processing happens.

### Chronicle Edge (Raspberry Pi / Any Small Device)
A lightweight relay that captures audio from local devices (OMI wearable, microphones, USB audio) and forwards it to Chronicle Core. Discovers the backend automatically via minidisc. Near-zero configuration.

### Phone App
Connects to Chronicle Core via Tailscale. Pairs using QR code scan from the web dashboard. No minidisc needed — the QR code contains the backend URL.

## What This Changes

| Layer | Today | With Edge Deployment |
|---|---|---|
| **Pi relay** | HAVPE relay, manually configured `--backend-url` | Chronicle Edge — discovers backend via minidisc, zero config |
| **Backend** | Wizard asks 15+ questions about IPs/URLs | Wizard asks: auth + API keys. That's it. |
| **Phone** | Manually enter backend URL | QR code scan (already exists) |
| **Adding a new Pi** | Clone repo, edit config, point at backend | Install Chronicle Edge, join Tailnet, done |
| **Service discovery** | `.env` files with hardcoded URLs | minidisc advertise/discover |
| **Inter-service URLs** | Manual SPEAKER_SERVICE_URL, PARAKEET_ASR_URL | Auto-discovered at startup |

## Setup Flow

```bash
# On AI workstation (one time)
git clone chronicle && cd chronicle
./wizard.sh          # Only asks: email, password, API keys
./start.sh           # Advertises via minidisc automatically

# On Raspberry Pi (any number of them)
pip install chronicle-edge   # or: curl | sh
chronicle-edge               # Finds backend via minidisc, starts relaying

# On phone
# Scan QR code from web dashboard -> connected
```

## Requirements

- **Tailscale** on all machines (provides encrypted networking + peer discovery)
- **minidisc** for service discovery (Python library, ~14KB, pydantic dependency)
- Tailscale socket access (`/var/run/tailscale/tailscaled.sock`) on machines running minidisc

## Further Reading

- [minidisc-integration.md](minidisc-integration.md) — Service discovery layer design
- [chronicle-edge.md](chronicle-edge.md) — Edge relay specification
- [prior-art.md](prior-art.md) — Research on NullClaw, OpenClaw, and minidisc
