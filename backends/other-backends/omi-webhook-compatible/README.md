# OMI-Webhook-Compatible Backend

A drop-in replacement backend compatible with the official OMI app webhook system. This backend provides seamless migration from the official OMI infrastructure while maintaining compatibility with existing OMI mobile app configurations.

## Features

- **OMI App Compatibility**: Works directly with the official OMI mobile app
- **Webhook Interface**: Receives audio via HTTP webhook endpoints
- **Audio Storage**: Saves audio recordings for processing and analysis
- **ngrok Integration**: Provides public webhook endpoints for mobile access
- **Simple Deployment**: Docker-based setup with minimal configuration

## Quick Start

### 1. Configure Environment

```bash
# Copy environment template
cp .env.template .env

# Edit .env file and add your ngrok token
NGROK_AUTHTOKEN=your_ngrok_token_here
```

### 2. Start the Service

```bash
# Start all services with Docker
docker compose up --build -d

# Check service status
docker compose ps
```

### 3. Configure OMI App

1. **Open the OMI app** on your mobile device
2. **Enable Developer Mode** in app settings
3. **Set Audio Bytes Webhook URL**:
   - Get the ngrok URL from logs: `docker compose logs ngrok`
   - Add webhook suffix: `<NGROK_URL>/webhook/audio_bytes`
   - Example: `https://abc123.ngrok.io/webhook/audio_bytes`
4. **Set Recording Duration**: 5-10 seconds for testing
5. **Save Configuration**

### 4. Test Audio Recording

Start recording with your OMI device. Audio files will be saved to the `audio_recordings/` directory.

## Configuration

### Environment Variables

```bash
# Required: ngrok authentication token
NGROK_AUTHTOKEN=your_ngrok_token_here

# Optional: Custom ngrok domain
NGROK_DOMAIN=your_custom_domain.ngrok.io

# Optional: Service configuration
WEBHOOK_PORT=8000
STORAGE_PATH=./audio_recordings
```

### ngrok Setup

1. **Create ngrok account** at https://ngrok.com
2. **Get authentication token** from ngrok dashboard
3. **Add token to .env file**

For production deployments, consider getting a custom ngrok domain for consistent webhook URLs.

## API Reference

### Webhook Endpoints

#### POST /webhook/audio_bytes
Receives audio data from OMI app.

**Request Format:**
- Content-Type: `audio/wav` or `audio/mpeg`
- Body: Binary audio data

**Response:**
- 200 OK: Audio received and saved successfully
- 400 Bad Request: Invalid audio format
- 500 Internal Server Error: Processing failed

## File Storage

Audio recordings are stored in the `audio_recordings/` directory with the following naming convention:
```
audio_recordings/
├── recording_YYYYMMDD_HHMMSS.wav
├── recording_YYYYMMDD_HHMMSS.wav
└── ...
```

## Integration

### With Chronicle Backend
For full transcription and memory features, use the [Chronicle Backend](../../../backend/) instead, which provides:
- Real-time transcription
- Memory extraction
- Speaker recognition
- Web dashboard

### With ASR Services
Audio files can be manually processed using [ASR Services](../../../extras/asr-services/) for transcription.

## Monitoring

### Check Service Status
```bash
# View all service logs
docker compose logs -f

# View specific service logs
docker compose logs webhook-backend
docker compose logs ngrok

# Check audio recordings
ls -la audio_recordings/
```

### ngrok Web Interface
Access ngrok web interface at http://localhost:4040 to monitor:
- Active tunnel status
- Request/response logs
- Bandwidth usage

## Troubleshooting

### Common Issues

**ngrok Authentication Failed:**
- Verify NGROK_AUTHTOKEN in .env file
- Check token validity on ngrok dashboard
- Ensure token has proper permissions

**OMI App Connection Failed:**
- Verify webhook URL format includes `/webhook/audio_bytes`
- Check ngrok tunnel is active
- Test webhook endpoint manually

**No Audio Files Created:**
- Check docker container logs for errors
- Verify webhook endpoint is receiving requests
- Check file permissions on audio_recordings directory

### Manual Testing

Test webhook endpoint with curl:
```bash
# Get ngrok URL
NGROK_URL=$(docker compose logs ngrok | grep "https://.*ngrok.io" | tail -1)

# Test webhook
curl -X POST "${NGROK_URL}/webhook/audio_bytes" \
     -H "Content-Type: audio/wav" \
     --data-binary @test_audio.wav
```

### Debug Mode

Enable verbose logging:
```bash
# Add to .env file
DEBUG=true

# Restart services
docker compose down && docker compose up -d
```

## Migration from Official OMI

1. **Stop using official OMI webhook** in app settings
2. **Deploy this backend** following setup instructions
3. **Update webhook URL** in OMI app to point to your ngrok URL
4. **Test audio recording** to verify migration success

## Limitations

- **No Real-time Processing**: Audio is stored but not automatically transcribed
- **Manual File Management**: Audio files require manual processing
- **Basic Webhook Interface**: Limited compared to Chronicle backend features
- **ngrok Dependency**: Requires ngrok for public access

## Related Documentation

- **[Chronicle Backend](../../../backend/)**: Full-featured alternative with transcription
- **[Simple Backend](../../simple/)**: WebSocket-based audio streaming
- **[ASR Services](../../../extras/asr-services/)**: Offline transcription services
