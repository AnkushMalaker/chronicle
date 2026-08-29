# HAVPE Relay (Home Assistant Voice Preview Edition Relay)

TCP-to-WebSocket relay that bridges ESP32 Voice-PE devices to the Chronicle backend.

## Architecture

```
ESP32 Voice-PE ──TCP:8989──► HAVPE Relay ──WebSocket──► Chronicle Backend
  (32-bit stereo)            (16-bit mono Opus)          (/ws/audio)
```

The relay:
- Listens for raw TCP audio from an ESP32 running ESPHome
- Converts 32-bit stereo I2S data to 16-bit mono PCM
- Authenticates with the Chronicle backend (JWT)
- Adapts the device-local JSONL/PCM stream to Chronicle audio v2
- Terminates Chronicle audio v2 and reports native half-duplex capabilities
- Delivers bound response WAVs through ESPHome and ACKs physical playback state

Interactive output requires both the updated relay and the included firmware from
the same revision. During an announcement the firmware stops microphone capture,
publishes `started/stopped` through ESPHome, then restores capture. A relay paired
with older firmware remains capture-only; it does not fall back to `play-audio`.

## Quick Start

### 1. Configure

```bash
cd extras/havpe-relay
./init.sh
```

The setup wizard configures:
- Backend URL and WebSocket URL
- Authentication credentials (reads defaults from backend `.env`)
- Device name and TCP port
- (Optional) ESP32 firmware WiFi and relay IP secrets

### 2. Flash the ESP32 Firmware

See [Firmware Flashing](#firmware-flashing) below.

### 3. Start the Relay

```bash
# With Docker
docker compose up --build -d

# Or run directly
uv run python main.py
```

## Firmware Flashing

The `firmware/` directory contains the ESPHome configuration for the ESP32-S3 Voice-PE.

### Configure Secrets

If you didn't configure firmware during `./init.sh`, create the secrets file manually:

```bash
cd firmware
cp secrets.template.yaml secrets.yaml
```

Edit `secrets.yaml` with your values:

```yaml
wifi_ssid: "YourWiFiNetwork"
wifi_password: "YourWiFiPassword"
relay_ip_address: "192.168.0.108"   # IP of the machine running this relay
```

### Flash

Connect the ESP32-S3 Voice-PE via USB, then:

```bash
./flash.sh
```

This installs ESPHome via the `firmware` dependency group and runs `esphome run`. On first flash ESPHome will:
1. Download and compile the ESP-IDF framework (~5 min first time)
2. Build the firmware
3. Flash over USB (select the serial port when prompted)

Subsequent flashes are faster (incremental builds) and can be done over WiFi (OTA).

To view device logs:

```bash
./flash.sh logs
```

### Hardware Wiring

The ESPHome config (`voice-chronicle.yaml`) expects an I2S microphone on these pins:

| Signal | GPIO |
|--------|------|
| BCLK   | 13   |
| LRCLK  | 14   |
| DIN    | 15   |

These match the default Voice-PE board pinout. If your board differs, edit the pin numbers in `voice-chronicle.yaml`.

### Verify Connection

After flashing, the ESP32 will:
1. Connect to WiFi
2. Open a TCP socket to `relay_ip_address:8989`
3. Stream raw I2S audio data

Check the relay logs to confirm audio is flowing:

```bash
# Docker
docker compose logs -f

# Direct
uv run python main.py -v
```

You should see `TCP client connected` followed by chunk processing messages.

## Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKEND_URL` | `http://host.docker.internal:8000` | Backend HTTP URL (for auth) |
| `BACKEND_WS_URL` | `ws://host.docker.internal:8000` | Backend WebSocket URL |
| `CHRONICLE_API_KEY` | — | Long-lived Chronicle API key (webui → Settings → API Keys, or minted by `init.py`) |
| `DEVICE_NAME` | `havpe` | Device identifier (becomes part of client ID) |
| `TCP_PORT` | `8989` | TCP port to listen on for ESP32 |

### Command Line Options

```bash
uv run python main.py --help
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | 8989 | TCP port for ESP32 connections |
| `--host` | `0.0.0.0` | Host address to bind to |
| `--backend-url` | from env | Backend API URL |
| `--backend-ws-url` | from env | Backend WebSocket URL |
| `--username` | from env | Auth username |
| `--password` | from env | Auth password |
| `--debug-audio` | off | Save raw audio to `audio_chunks/` |
| `-v` / `-vv` | WARNING | Increase log verbosity |

## Project Structure

```
havpe-relay/
├── main.py                        # Relay server
├── init.py                        # Setup wizard
├── init.sh                        # Setup wizard wrapper
├── flash.sh                       # Firmware flash wrapper
├── .env.template                  # Environment template
├── docker-compose.yml             # Docker config
├── Dockerfile                     # Container build
├── firmware/
│   ├── voice-chronicle.yaml       # ESPHome config for ESP32-S3
│   ├── chronicle-sdk/chronicle.h  # Device-local JSONL/PCM TCP client
│   ├── secrets.template.yaml      # Secrets template
│   └── secrets.yaml               # Your secrets (gitignored)
└── pyproject.toml                 # Python dependencies
```

## Troubleshooting

### ESP32 won't connect to relay
- Verify `relay_ip_address` in `firmware/secrets.yaml` matches this machine's LAN IP
- Ensure the relay is running and port 8989 is not firewalled
- Check ESP32 serial logs: `esphome logs firmware/voice-chronicle.yaml`

### Authentication failures
- Verify credentials: try logging in at `BACKEND_URL/docs` with the same email/password
- Check the backend is reachable from the relay host

### No audio in Chronicle
- Run with `-v` to confirm chunks are being sent
- Run with `--debug-audio` to save raw audio locally and verify it's not silence
- Check backend WebSocket logs for the connection
