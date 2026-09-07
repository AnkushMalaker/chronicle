# Chronicle Backends

The primary Chronicle backend lives in [`../backend/`](../backend/). This directory contains alternative implementations and the Helm chart.

## Available Backends

### [Chronicle Backend](../backend/) (Recommended)
Full-featured backend with comprehensive AI capabilities:
- Real-time audio processing and transcription
- Memory system with conversation analysis
- Speaker recognition and enrollment
- Web UI for management and monitoring
- RESTful API with WebSocket support
- MongoDB and FalkorDB integration

### [Simple Backend](simple/)
Lightweight backend for basic audio capture:
- WebSocket audio streaming
- Opus to PCM conversion and WAV storage
- Wyoming protocol support
- Minimal dependencies and resource usage

### [Other Backends](other-backends/)
Additional backend implementations:
- **OMI-Webhook-Compatible**: Drop-in replacement for official OMI backend
- **Example Satellite**: Wyoming protocol satellite for distributed setups

## Audio Processing

All backends expect audio in Opus format streamed via WebSocket. The audio processing flow typically involves:

1. **Audio Reception**: Receive Opus-encoded audio from mobile clients
2. **Format Conversion**: Decode Opus to PCM using OmiSDK
3. **Storage/Processing**: Save audio files and/or process for transcription
4. **Integration**: Connect with ASR services for speech-to-text conversion

## ASR Integration

Backends can integrate with transcription services from the [ASR Services](../extras/asr-services/) directory for speech-to-text conversion.

## Getting Started

Each backend includes its own README with specific setup instructions:
- [Chronicle Backend Setup](../backend/README.md)
- [Simple Backend Setup](simple/README.md)
- [OMI-Webhook-Compatible Setup](other-backends/omi-webhook-compatible/README.md)
- [Example Satellite Setup](other-backends/example-satellite/README.md)

## Choosing a Backend

**For production use**: Start with the Chronicle Backend for full features and scalability.

**For testing**: Use the Simple Backend to understand basic audio processing.

**For OMI migration**: Use the OMI-Webhook-Compatible backend as a drop-in replacement.
