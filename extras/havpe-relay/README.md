# HAVPE Relay (Home Assistant Voice Preview Edition Relay)

TCP-to-WebSocket relay for ESPHome Voice-PE that connects to the Omi advanced backend.

## Features

- **TCP Server**: Listens on port 8989 for ESP32 Voice-PE connections
- **Audio Format Conversion**: Converts 32-bit PCM to 16-bit PCM using easy-audio-interfaces
- **WebSocket Client**: Forwards converted audio to backend at `/ws?codec=pcm` endpoint
- **Graceful Handling**: Supports reconnections and proper cleanup
- **Configurable**: Command-line options for ports and endpoints

## Audio Processing

- **Input Format**: 32-bit PCM, 16kHz, 2 channels (from ESP32 Voice-PE)
- **Output Format**: 16-bit PCM, 16kHz, 2 channels (to backend)
- **Conversion**: Uses easy-audio-interfaces for robust audio processing

## Installation

Make sure you're in the havpe-relay directory:

```bash
cd havpe-relay
```

Install dependencies (already configured in pyproject.toml):

```bash
uv sync
```

## Usage

### Basic Usage

Start the relay with default settings:

```bash
uv run main.py
```

This will:
- Listen for TCP connections on port 8989
- Forward to WebSocket at `ws://127.0.0.1:8000/ws?codec=pcm`

### Advanced Usage

```bash
# Custom TCP port
uv run main.py --tcp-port 9090

# Custom WebSocket URL
uv run main.py --ws-url "ws://192.168.1.100:8000/ws?codec=pcm"

# Verbose logging
uv run main.py -v    # INFO level
uv run main.py -vv   # DEBUG level

# Full configuration example
uv run main.py --tcp-port 8989 --ws-url "ws://localhost:8000/ws?codec=pcm" -v
```

### Command Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--tcp-port` | 8989 | TCP port to listen on for ESP32 connections |
| `--ws-url` | `ws://127.0.0.1:8000/ws?codec=pcm` | WebSocket URL to forward audio to |
| `-v` / `--verbose` | WARNING | Increase verbosity (-v: INFO, -vv: DEBUG) |

## Architecture

```
ESP32 Voice-PE → TCP:8989 → HAVPE Relay → WebSocket:/ws?codec=pcm → Omi Backend
     (32-bit PCM)                    (16-bit PCM)
```

## Integration with Backend

The relay automatically includes the following WebSocket parameters when connecting to the backend:

- `user_id=esp32_voice_pe` - Identifies the audio source
- `rate=16000` - Sample rate (16kHz)
- `width=2` - Sample width (16-bit = 2 bytes)
- `channels=2` - Stereo audio
- `src=voice_pe` - Source identifier

Example WebSocket URL sent to backend:
```
ws://127.0.0.1:8000/ws?codec=pcm?user_id=esp32_voice_pe&rate=16000&width=2&channels=2&src=voice_pe
```

## Development

### Project Structure

```
havpe-relay/
├── main.py           # Main relay implementation
├── pyproject.toml    # Project configuration
├── uv.lock          # Dependency lock file
├── README.md        # This file
├── .python-version  # Python version (3.12)
└── .venv/           # Virtual environment
```

### Dependencies

- `easy-audio-interfaces>=0.2.6` - Audio processing and format conversion
- `websockets>=15.0.1` - WebSocket client implementation
- Python 3.12+ required

### Audio Conversion Details

The relay uses a two-step process for audio conversion:

1. **Input Processing**: Wraps incoming TCP data in `AudioChunk` format
2. **Format Conversion**: Converts 32-bit float PCM to 16-bit integer PCM
   - Clamps values to [-1, 1] range
   - Scales to 16-bit integer range (-32767 to 32767)
   - Maintains sample rate and channel count

## Troubleshooting

### Common Issues

1. **Connection Refused**: Ensure the backend is running on the specified WebSocket URL
2. **TCP Port in Use**: Another service might be using port 8989
3. **Audio Quality Issues**: Check that ESP32 is sending 32-bit PCM data

### Debug Mode

Run with debug logging to see detailed audio processing:

```bash
uv run main.py -vv
```

This will show:
- TCP connection details
- Audio chunk sizes and conversion rates
- WebSocket message sizes
- Error details

### Monitoring

Watch the logs for:
- `TCP client connected` - ESP32 successfully connected
- `WebSocket connected` - Backend connection established
- `Relayed X bytes (32-bit) -> Y bytes (16-bit)` - Audio being processed
- Conversion ratio should be approximately 2:1 (32-bit to 16-bit)

## Testing

You can test the relay using the provided test listener (if needed):

1. Start the test WebSocket listener on port 8000
2. Start the relay: `uv run main.py -v`
3. Connect your ESP32 Voice-PE device to the relay on port 8989

## License

This project is part of the chronicle ecosystem.
