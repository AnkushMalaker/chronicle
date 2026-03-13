# ASR Services Quick Start

Get offline speech recognition running in 5 minutes.

## Prerequisites

- Docker and Docker Compose installed
- At least 4GB RAM available
- Audio files or microphone for testing

## Start Service

### Parakeet ASR (Available)
```bash
# Navigate to ASR services directory
cd extras/asr-services

# Start Parakeet service (GPU required)
docker compose up parakeet-asr -d

# Verify service is running
docker compose logs parakeet-asr
```

### Moonshine ASR (Currently Disabled)
```bash
# Note: Moonshine is commented out in docker-compose.yml
# To enable: uncomment lines 25-47 and change port to 8766 to avoid conflicts
# Then: docker compose up moonshine-asr -d
```

## Test Transcription

### Using Test Client
```bash
# Test with sample audio file
python client.py --host localhost --port 8765 --audio test.wav

# Or use microphone input
python client.py --host localhost --port 8765 --microphone
```

### Integration with Chronicle
Set the Parakeet ASR URL in your Chronicle backend:
```bash
# In your .env file
PARAKEET_ASR_URL=http://localhost:8765
```

## Verify Setup

1. **Service Health**: `docker compose logs parakeet-asr`
2. **Port Listening**: `netstat -an | grep 8765`
3. **Test Transcription**: Use client.py with sample audio

## Next Steps

1. **Configure Backend**: Update Chronicle to use offline ASR as fallback
2. **Test Integration**: Verify transcription works when Deepgram is unavailable
3. **Performance Tuning**: Monitor CPU/memory usage and adjust as needed
4. **Production Deploy**: Scale services based on load requirements

## Quick Troubleshooting

- **Port conflicts**: Change ports in docker-compose.yml
- **Service won't start**: Check `docker compose logs [service]`
- **No transcription**: Verify audio format (PCM, 16-bit recommended)
- **Poor quality**: Check audio input levels and background noise

## Configuration

For basic setup, defaults work fine. For production:
- Set appropriate resource limits in docker-compose.yml
- Configure multiple workers for high load
- Enable GPU acceleration for Parakeet if available
