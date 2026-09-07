# Simple Backend

A lightweight WebSocket audio service for basic audio capture and storage. This backend provides minimal audio processing focused on receiving Opus-encoded audio streams and saving them as WAV files.

## Features

- **WebSocket Audio Streaming**: Accepts Opus packets over WebSocket (`/ws`)
- **Wyoming Protocol Support**: Handles Wyoming protocol events with fallback to raw audio
- **Audio Processing**: Converts Opus to 16kHz/16-bit/mono PCM using OmiSDK
- **Automatic Segmentation**: Creates 30-second WAV chunks for storage
- **Session Management**: Tracks audio sessions with start/stop events

## Quick Start

### Using Docker (Recommended)

```bash
# Start the service
docker compose up --build

# Audio will be saved to ./audio_chunks/
# Service available at ws://localhost:8000/ws
```

### Local Development

```bash
# Install dependencies
uv sync

# Run the service
uv run python main.py

# Service starts at ws://localhost:8000/ws
```

## Configuration

### Environment Variables

```bash
HOST=0.0.0.0          # Server host (default: 0.0.0.0)
PORT=8000             # Server port (default: 8000)
```

### Ngrok Tunneling (Optional)

```bash
NGROK_AUTHTOKEN=your_token    # Ngrok authentication token
NGROK_DOMAIN=your_domain      # Custom Ngrok domain
```

## Audio Processing

### Input Format
- **Protocol**: Wyoming protocol or raw binary
- **Audio Codec**: Opus packets
- **Client Support**: Any device streaming Opus audio

### Output Format
- **File Format**: WAV files
- **Audio Settings**: 16kHz sample rate, 16-bit depth, mono channel
- **Segmentation**: 30-second chunks
- **Storage**: `./audio_chunks/` directory

### File Naming
```
client{id}_{timestamp}_{segment}.wav
# Example: client1_20240101_143022_0.wav
```

## Wyoming Protocol

The service supports Wyoming protocol events:

- **audio-start**: Begins audio session
- **audio-chunk**: Contains Opus audio data
- **audio-stop**: Ends audio session and flushes remaining audio

For clients not using Wyoming protocol, raw Opus packets are accepted with automatic session management.

## API Reference

### WebSocket Endpoint

**Endpoint**: `/ws`

**Message Format** (Wyoming Protocol):
```json
{"type": "audio-start", "data": {"rate": 16000, "width": 2, "channels": 1}}
{"type": "audio-chunk", "data": {}, "payload_length": 320}
<binary_opus_data>
{"type": "audio-stop", "data": {"timestamp": 1234567890}}
```

**Fallback**: Raw Opus binary data (backward compatibility)

## Integration

### Mobile Apps
Connect to WebSocket endpoint and stream Opus audio:

```javascript
const ws = new WebSocket('ws://localhost:8000/ws');

// Send audio data
ws.send(opusAudioData);
```

### Chronicle Backend
For full transcription and memory features, use the [Chronicle Backend](../../backend/) instead.

## Monitoring

### Logs
```bash
# View service logs
docker compose logs -f chronicle-backend

# Check audio processing
tail -f ./audio_chunks/
```

### Health Check
The service provides basic logging for connection and audio processing status.

## Limitations

- **No Transcription**: Audio is only saved, not transcribed
- **No Memory Processing**: No conversation memory or LLM integration
- **Basic Session Management**: Simple session tracking without advanced features
- **Storage Only**: Files are saved but not indexed or searchable

## Troubleshooting

### Common Issues

**Connection Issues**:
- Verify WebSocket endpoint: `ws://localhost:8000/ws`
- Check firewall and port availability

**Audio Quality**:
- Ensure Opus encoding on client side
- Check audio chunk size and timing

**Storage Issues**:
- Verify `./audio_chunks/` directory permissions
- Monitor disk space for continuous recording

### Debug Mode
```bash
# Enable debug logging
export LOG_LEVEL=DEBUG
uv run python main.py
```

## Architecture

```
Client (Opus) → WebSocket → OmiOpusDecoder → PCM Buffer → WAV Files
```

### Dependencies
- **FastAPI**: WebSocket server framework
- **OmiSDK**: Opus audio decoding
- **Python 3.12+**: Runtime environment

## Related Services

- **[Chronicle Backend](../../backend/)**: Full-featured backend with transcription and memory
- **[Mobile App](../../app/)**: React Native client for audio streaming
- **[Speaker Recognition](../../extras/speaker-recognition/)**: Speaker identification service
