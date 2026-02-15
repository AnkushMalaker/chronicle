# Local Wearable Client

macOS client that scans for BLE wearable devices (OMI, Neo1, Friend), connects, and streams audio to the Chronicle backend. Runs as a **menu bar app** with device selection, or headless for background use.

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
```

## Architecture

- **Main thread**: rumps menu bar app (AppKit event loop)
- **Background thread**: asyncio event loop running bleak BLE scanning/connecting/streaming
- Communication via `asyncio.run_coroutine_threadsafe()` (menu to BLE) and a shared lock-protected state object (BLE to menu, polled every 2s)
