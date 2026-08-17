# Chronicle Edge — Lightweight Relay Specification

## Concept

Chronicle Edge is a lightweight relay that runs on small devices (Raspberry Pi, old laptops, any Linux box) to capture audio and forward it to Chronicle Core. It replaces the current HAVPE relay with a more general, zero-config solution.

## Design Goals

1. **Zero config** — join Tailnet, run binary, it finds the backend
2. **Tiny footprint** — minimal dependencies, runs on a Pi Zero
3. **Multiple audio sources** — OMI BLE, USB mic, local mic, file watch
4. **Auto-discovery** — finds Chronicle Core via minidisc
5. **Resilient** — reconnects on network changes, buffers during outages

## What It Does

```
Audio Sources              Chronicle Edge              Chronicle Core
+------------+            +----------------+          +----------------+
| OMI Device |--BLE------>|                |          |                |
+------------+            |  Audio Capture |  Tailscale  |  Backend API |
                          |       +        |--------->|  /ws endpoint  |
+------------+            |  Auth Manager  |  minidisc|                |
| USB Mic    |--ALSA----->|       +        |  discovers|  RQ Workers   |
+------------+            |  WS Forwarder  |          |                |
                          |       +        |          |  MongoDB etc.  |
+------------+            |  Health Check  |          |                |
| File Watch |--inotify-->|                |          |                |
+------------+            +----------------+          +----------------+
```

## Core Responsibilities

### 1. Audio Capture
- **OMI BLE**: Scan for OMI devices, pair, receive Opus audio (existing HAVPE relay logic)
- **Local microphone**: ALSA/PulseAudio capture for non-OMI setups
- **File watch**: Monitor a directory for new audio files, forward them via upload API

### 2. Backend Discovery
- Use minidisc to find `chronicle-backend` on the Tailnet
- Cache the discovered endpoint, re-discover on connection failure
- Fall back to `--backend-url` CLI arg or `CHRONICLE_BACKEND_URL` env var

### 3. Authentication
- On first run, authenticate with Chronicle Core (email/password or device token)
- Store credentials locally (encrypted)
- Handle token refresh automatically

### 4. Audio Forwarding
- WebSocket connection to backend `/ws` endpoint (Wyoming protocol)
- Automatic reconnection with exponential backoff
- Optional: local buffering during network outages (write to disk, forward when reconnected)

### 5. Health Reporting
- Advertise itself via minidisc as `chronicle-edge` with device metadata labels
- Expose local `/health` endpoint for monitoring
- Report connection status to backend

## Setup Flow

### First Run

```bash
# Install
pip install chronicle-edge
# or
curl -sSL https://chronicle.ai/install-edge.sh | sh

# Run (auto-discovers backend on Tailnet)
chronicle-edge

# First time prompts:
#   Chronicle backend found at 100.64.1.5:8000
#   Email: user@example.com
#   Password: ********
#   Device name [raspberry-pi]: living-room
#   Authenticated. Credentials saved.
#   Scanning for OMI devices...
#   Found: OMI-ABC123
#   Streaming audio to chronicle-backend...
```

### Subsequent Runs

```bash
chronicle-edge
# Chronicle backend found at 100.64.1.5:8000
# Authenticated as user@example.com (living-room)
# Connected to OMI-ABC123
# Streaming...
```

### As a System Service

```bash
chronicle-edge install    # Install systemd service
chronicle-edge start      # Start daemon
chronicle-edge status     # Check status
chronicle-edge stop       # Stop daemon
```

Inspired by NullClaw's `service install/start/stop/status` pattern.

## Configuration

Minimal config file at `~/.chronicle-edge/config.yml`:

```yaml
# Auto-populated on first run
backend_url: null              # null = use minidisc discovery
device_name: living-room
audio_source: omi              # omi | mic | file_watch

# Optional overrides
omi:
  device_filter: "OMI-*"      # BLE name filter
mic:
  device: default              # ALSA device
  sample_rate: 16000
file_watch:
  directory: ~/audio-inbox
  delete_after_upload: true

# Credentials (encrypted)
credentials_file: ~/.chronicle-edge/credentials.enc
```

## Comparison with Current HAVPE Relay

| Aspect | HAVPE Relay (Current) | Chronicle Edge (Proposed) |
|---|---|---|
| **Config** | `--backend-url`, `--backend-ws-url`, env vars | Zero-config with minidisc, optional overrides |
| **Discovery** | Manual URL | Automatic via minidisc |
| **Auth** | Env vars `AUTH_USERNAME`, `AUTH_PASSWORD` | Interactive first-run, stored encrypted |
| **Audio sources** | OMI BLE only (ESP32 bridge) | OMI BLE, mic, file watch |
| **Install** | Clone repo, pip install deps | `pip install chronicle-edge` or curl script |
| **Daemon** | Manual (screen, tmux, systemd) | Built-in `install/start/stop` commands |
| **Reconnection** | Basic | Exponential backoff + local buffering |
| **Multi-device** | One relay per device | One Edge per Pi, multiple audio sources |

## Implementation Path

### Phase 1: minidisc Integration (Minimal)
- Add minidisc discovery to existing HAVPE relay
- Backend advertises via minidisc on startup
- Relay finds backend without `--backend-url`

### Phase 2: Chronicle Edge Package
- Extract relay logic into standalone `chronicle-edge` package
- Add systemd service management
- Interactive first-run setup
- Encrypted credential storage

### Phase 3: Multi-Source Audio
- Add local microphone capture
- Add file watch mode
- Source selection via config

### Phase 4: Resilience
- Local audio buffering during outages
- Connection health monitoring
- Automatic OMI device reconnection

## Tech Stack

- **Python** — matches Chronicle backend, shared auth logic
- **minidisc-python** — service discovery
- **bleak** — BLE for OMI devices (already used by HAVPE relay)
- **websockets** — WebSocket client for backend connection
- **pydantic** — config validation (already a minidisc dependency)
- **keyring** or **cryptography** — credential storage
