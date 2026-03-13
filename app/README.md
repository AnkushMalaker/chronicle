# Chronicle Mobile App

React Native mobile application for connecting OMI devices and streaming audio to Chronicle backends. Supports cross-platform deployment on iOS and Android with Bluetooth integration.

## Features

- **OMI Device Integration**: Connect via Bluetooth and stream audio
- **Phone Audio Streaming**: Use phone's microphone directly (NEW)
- **Cross-Platform**: iOS and Android support using React Native
- **Real-time Audio Streaming**: OPUS audio transmission to backend services
- **WebSocket Communication**: Efficient real-time data transfer
- **Backend Selection**: Configure connection to any compatible backend
- **Live Audio Visualization**: Real-time audio level meters

## Quick Start

### Prerequisites

- Node.js 18+ and npm installed
- Expo CLI: `npm install -g @expo/cli`
- **iOS**: Xcode and iOS Simulator or physical iOS device
- **Android**: Android Studio and Android device/emulator

### Installation

```bash
# Navigate to app directory
cd app

# Install dependencies
npm install

# Start development server
npm start
```

## Platform-Specific Setup

### iOS Development

#### Method 1: Expo Development Build (Recommended)

```bash
# Clean and prebuild
npx expo prebuild --clean

# Install development client
npx expo install expo-dev-client

# Start development server
npx expo start --dev-client

# Run on iOS device
npx expo run:ios --device
```

#### Method 2: Xcode Development

```bash
# Prebuild for iOS
npx expo prebuild --clean

# Install iOS dependencies
cd ios && pod install && cd ..

# Open in Xcode
open ios/chronicle.xcworkspace
```

Build and run from Xcode interface.

### Android Development

```bash
# Build and run on Android device
npx expo run:android --device
```

#### Android Network Configuration

For local development, configure network permissions:

**Development (Local Backend):**
Add to `android/app/src/main/AndroidManifest.xml`:
```xml
<application
    android:usesCleartextTraffic="true"
    ... >
```

**Production (HTTPS Backend):**
Create `android/app/src/main/res/xml/network_security_config.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="true">your-backend-domain.com</domain>
    </domain-config>
</network-security-config>
```

Reference in `AndroidManifest.xml`:
```xml
<application
    android:networkSecurityConfig="@xml/network_security_config"
    ... >
```

## Backend Configuration

### Supported Backends

The app connects to any backend that accepts OPUS audio streams:

1. **Simple Backend** (`backends/simple/`)
   - Basic audio capture and storage
   - Good for testing and development
   - WebSocket endpoint: `/ws`

2. **Advanced Backend** (`backends/advanced/`)
   - Full transcription and memory features
   - Real-time processing with speaker recognition
   - WebSocket endpoint: `/ws?codec=pcm`

### Connection Setup

#### Local Development
```
Backend URL: ws://[machine-ip]:8000/ws?codec=pcm
Example: ws://192.168.1.100:8000/ws?codec=pcm
```

#### Public Access (Production)
Use ngrok or similar tunneling service:

```bash
# Start ngrok tunnel
ngrok http 8000

# Use provided URL in app
Backend URL: wss://[ngrok-subdomain].ngrok.io/ws?codec=pcm
```

### Configuration Steps

1. **Start your chosen backend** (see backend-specific README)
2. **Open the mobile app**
3. **Navigate to Settings**
4. **Enter Backend URL**:
   - Local: `ws://[your-ip]:8000/ws?codec=pcm`
   - Public: `wss://[your-domain]/ws?codec=pcm`
5. **Save configuration**

## Phone Audio Streaming (NEW)

### Overview
Stream audio directly from your phone's microphone to Chronicle backend, bypassing Bluetooth devices. This feature provides a direct audio input method for users who want to use their phone as the audio source.

### Features
- **Direct Microphone Access**: Use phone's built-in microphone
- **Real-time Audio Streaming**: Live audio processing with visualization
- **Seamless Integration**: Switch between Bluetooth and phone audio modes
- **Cross-Platform**: Works on both iOS and Android
- **Live Audio Meters**: Visual feedback showing audio levels in real-time

### Setup & Usage

#### Enable Phone Audio Streaming
1. **Open Chronicle app**
2. **Configure Backend Connection** (see Backend Configuration section)
3. **Grant Microphone Permissions** when prompted
4. **Tap "Stream Phone Audio" button** in main interface
5. **Start speaking** - audio streams in real-time to backend

#### Requirements
- **iOS**: iOS 13+ with microphone permissions
- **Android**: Android API 21+ with microphone permissions
- **Network**: Stable connection to Chronicle backend
- **Backend**: Advanced backend running with `/ws?codec=pcm` endpoint

#### Switching Audio Sources
- **Mutual Exclusion**: Cannot use Bluetooth and phone audio simultaneously
- **Automatic Detection**: App disables conflicting options when one is active
- **Visual Feedback**: Clear indicators show active audio source

### Troubleshooting Phone Audio

#### Audio Not Streaming
- **Check Permissions**: Ensure microphone access granted
- **Verify Backend URL**: Confirm `ws://[ip]:8000/ws?codec=pcm` format
- **Network Connection**: Test backend connectivity
- **Authentication**: Verify JWT token is valid

#### Poor Audio Quality
- **Check Signal Strength**: Ensure stable network connection
- **Reduce Background Noise**: Use in quiet environment
- **Restart Recording**: Stop and restart phone audio streaming

#### Permission Issues
- **iOS**: Settings > Privacy & Security > Microphone > Chronicle
- **Android**: Settings > Apps > Chronicle > Permissions > Microphone

#### No Audio Level Visualization
- **Restart App**: Close and reopen the application
- **Check Audio Input**: Ensure microphone is working in other apps
- **Backend Logs**: Verify backend is receiving audio data

## User Workflow

### Device Connection

1. **Enable Bluetooth** on your mobile device
2. **Open Chronicle app**
3. **Pair OMI device**:
   - Go to Device Settings
   - Scan for nearby OMI devices
   - Select your device from the list
   - Complete pairing process

### Audio Streaming

#### Option 1: Bluetooth Audio (OMI Device)
1. **Configure backend connection** (see Configuration Steps above)
2. **Test connection**:
   - Tap "Test Connection" in settings
   - Verify green status indicator
3. **Start recording**:
   - Press record button in main interface
   - Speak into OMI device
   - Audio streams to backend in real-time

#### Option 2: Phone Audio Streaming
1. **Configure backend connection** (see Configuration Steps above)
2. **Enable phone audio**:
   - Tap "Stream Phone Audio" button
   - Grant microphone permissions when prompted
3. **Start speaking**:
   - Speak directly into phone microphone
   - Watch real-time audio level visualization
   - Audio streams to backend automatically

### Monitoring

1. **Check connection status** in app header
2. **View real-time indicators**:
   - Audio level meters
   - Connection status
   - Battery level (if supported)
3. **Access backend dashboard** for processed results

## Troubleshooting

### Common Issues

**Bluetooth Connection Problems:**
- Ensure OMI device is in pairing mode
- Reset Bluetooth on mobile device
- Clear app cache and restart
- Check device compatibility

**Audio Streaming Issues:**
- Verify backend URL format (include `ws://` or `wss://`)
- Check network connectivity
- Test with simple backend first
- Monitor backend logs for connection attempts

**Phone Audio Streaming Issues:**
- Grant microphone permissions in device settings
- Ensure stable network connection to backend
- Restart phone audio streaming if no data flowing
- Check backend logs for audio data reception
- Verify JWT authentication token is valid

**Build Errors:**
- Clear Expo cache: `npx expo start --clear`
- Clean prebuild: `npx expo prebuild --clean`
- Reinstall dependencies: `rm -rf node_modules && npm install`

### Debug Mode

Enable detailed logging:
1. Go to app Settings
2. Enable "Debug Mode"
3. View console logs for connection details

### Network Testing

Test backend connectivity:
```bash
# Test WebSocket endpoint
curl -i -N -H "Connection: Upgrade" \
     -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Key: test" \
     -H "Sec-WebSocket-Version: 13" \
     http://[backend-ip]:8000/ws?codec=pcm
```

## Development

### Project Structure
```
app/
├── src/
│   ├── components/     # React Native components
│   ├── screens/        # App screens
│   ├── services/       # WebSocket and Bluetooth services
│   └── utils/          # Helper utilities
├── app.json           # Expo configuration
└── package.json       # Dependencies
```

### Key Dependencies
- **React Native**: Cross-platform mobile framework
- **Expo**: Development and build toolchain
- **React Native Bluetooth**: OMI device communication
- **WebSocket**: Real-time backend communication

### Building for Production

#### iOS App Store
```bash
# Build for iOS
npx expo build:ios

# Follow Expo documentation for App Store submission
```

#### Android Play Store
```bash
# Build for Android
npx expo build:android

# Generate signed APK for distribution
```

## Integration Examples

### WebSocket Communication
```javascript
// Connect to backend
const ws = new WebSocket('ws://backend-url:8000/ws?codec=pcm');

// Send audio data
ws.send(audioBuffer);

// Handle responses
ws.onmessage = (event) => {
  // Process transcription or acknowledgment
};
```

### Bluetooth Audio Capture
```javascript
// Start audio streaming from OMI device
await BluetoothService.startAudioStream();

// Handle audio data
BluetoothService.onAudioData = (audioBuffer) => {
  websocket.send(audioBuffer);
};
```

## Related Documentation

- **[Backend Setup](../backends/)**: Choose and configure backend services
- **[Quick Start Guide](../quickstart.md)**: Complete system setup
- **[Advanced Backend](../backends/advanced/)**: Full-featured backend option
- **[Simple Backend](../backends/simple/)**: Basic backend for testing
