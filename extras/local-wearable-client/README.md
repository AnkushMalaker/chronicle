# Local Wearable Client

macOS client that scans for BLE wearable devices (OMI, Neo1, Friend), connects, and streams audio to the Chronicle backend. Runs as a **menu bar app** with device selection, or headless for background use.

Pendant capture uses Chronicle audio v2. Speakerless OMI/Neo devices play through the tray host:
verified headphones keep capture live, while speakers gate capture during TTS. Elato
playback requires firmware implementing the bound BLE status contract; old Elato
firmware remains capture-only. See the
[audio WebSocket protocol](../../docs/backend/audio-websocket-protocol.md) for the
client matrix and [Elato firmware contract](../../docs/firmware/elato-speaker-protocol-v1.md).

> **Unified tray:** device scan/connect/stream now also lives as the *Pendant*
> section of the [Chronicle tray](../chronicle-tray/) (`chronicle-tray install
> --pendant`), which depends on this project's `chronicle_wearable` package.
> The rumps menu bar app below still works and remains the home of the macOS
> screen-capture settings UI.

## Prerequisites

- macOS (menu bar and launchd features are macOS-only)
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Opus codec library: `brew install opus`
- A configured `.env` file (copy from `.env.template`)

## Quick Start

```bash
cd extras/local-wearable-client
cp .env.template .env   # Edit with your backend credentials
./start.sh              # Launches menu bar app
```

## CLI Commands

```bash
./start.sh              # Menu bar app (default)
./start.sh menu         # Menu bar app (explicit)
./start.sh run          # Headless mode — scan, connect, stream in terminal
./start.sh scan         # One-shot scan — print nearby devices and exit
./start.sh install      # Install as macOS login service (launchd)
./start.sh uninstall    # Remove login service
./start.sh status       # Show service status
./start.sh logs         # Tail service log file
```

## Menu Bar App

Running `./start.sh` (or `./start.sh menu`) puts an icon in the macOS menu bar:

| Icon | Meaning |
|------|---------|
| `⊙` | Scanning / idle |
| `●` | Connected to a device |
| `⊘` | Error |

Click the icon to see:
- Connection status
- List of nearby devices (click to connect/disconnect)
- Actual host voice route and TTS playback/interruption state
- Voice output policy: Automatic, Require headphones, or Always speaker-safe
- "Scan Now" to trigger an immediate BLE scan

## Auto-Start on Login (launchd)

Install as a background service that starts automatically when you log in:

```bash
./start.sh install
```

This creates a launchd agent at `~/Library/LaunchAgents/com.chronicle.wearable-client.plist` that runs in headless mode (`run` subcommand). It reads your `.env` for backend credentials.

Logs go to `~/Library/Logs/Chronicle/wearable-client.log`.

```bash
./start.sh status       # Check if service is running
./start.sh logs         # Tail the log file
./start.sh uninstall    # Remove the service
```

## Configuration

### `.env` — Backend credentials

```bash
BACKEND_HOST=localhost:8000
USE_HTTPS=false
VERIFY_SSL=true
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your-password
```

### `devices.yml` — Known devices and scanning

```yaml
# Pin specific devices by MAC address
devices:
  - mac: "AA:BB:CC:DD:EE:FF"
    name: "my-neo1"
    type: "neo1"    # neo1 or omi

# Auto-discover any OMI/Neo/Friend device in range
auto_discover: true

# Seconds between scans when no device is connected
scan_interval: 10

# auto, require_headphones, or always_half_duplex
voice_output_policy: auto
```

`auto` is the default. The client treats a route as isolated only when macOS reports
an explicit headphone/earbud device name; generic Bluetooth and USB outputs remain
speaker-safe because they may feed speakers. Changing the policy or the default macOS
output ends the current voice session and starts a new capture epoch.

## Architecture

- **Main thread**: rumps menu bar app (AppKit event loop)
- **Background thread**: asyncio event loop running bleak BLE scanning/connecting/streaming
- Communication via `asyncio.run_coroutine_threadsafe()` (menu to BLE) and a shared lock-protected state object (BLE to menu, polled every 2s)
