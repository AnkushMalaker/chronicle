# Speaker Recognition Web UI

A React-based web interface for the speaker recognition system, converted from the original Streamlit application.

## Features

- **User Management**: Simple user selection with default admin user and ability to create new users
- **Audio Viewer**: Upload and visualize audio files with interactive waveform plots
- **Annotation Tool**: Label speaker segments with hash-based persistence
- **Speaker Enrollment**: Register speakers via file upload or guided recording
- **Speaker Management**: Manage enrolled speakers and view metrics
- **Speaker Inference**: Identify speakers in new audio files
- **Live Inference**: Real-time transcription and speaker identification

## Technology Stack

- **Frontend**: React 18 + TypeScript
- **Build Tool**: Vite
- **Styling**: Tailwind CSS
- **Charts**: Plotly.js
- **Routing**: React Router v6
- **HTTP Client**: Axios
- **File Processing**: Web Audio API, Spark-MD5

## Development

### Prerequisites

- Node.js 16 or higher
- npm or yarn
- Speaker recognition backend service running on port 8001

### Installation

```bash
cd webui
npm install
```

### Development Server

```bash
npm run dev
```

The application will start on `http://localhost:3000` with API proxy to `http://localhost:8001`.

### Build for Production

```bash
npm run build
```

### Project Structure

```
src/
├── components/          # Reusable UI components
│   ├── layout/         # Layout components
│   ├── FileUploader.tsx
│   ├── WaveformPlot.tsx
│   └── UserSelector.tsx
├── contexts/           # React contexts for state management
│   └── UserContext.tsx
├── pages/              # Main application pages
│   ├── AudioViewer.tsx
│   ├── Annotation.tsx
│   ├── Enrollment.tsx
│   ├── Speakers.tsx
│   └── Inference.tsx
├── services/           # API and data services
│   ├── api.ts
│   └── database.ts
├── utils/              # Utility functions
│   ├── audioUtils.ts
│   └── fileHash.ts
├── App.tsx             # Main application component
└── main.tsx            # Application entry point
```

## Features Implemented

### ✅ Completed
- Basic React application setup with TypeScript
- User management with localStorage fallback
- Audio file upload and hash calculation
- Waveform visualization with Plotly.js
- Interactive click-to-play waveform
- Audio playback and export functionality
- Responsive layout with Tailwind CSS
- Annotation interface with timeline and speaker labeling
- Audio hash-based annotation persistence
- Speaker enrollment workflows (single and batch)
- Direct microphone recording with WebM to WAV conversion
- Speaker management interface with metrics tracking
- Speaker identification/inference with confidence scores
- Direct enrollment from annotation segments
- Append vs Replace enrollment options
- Real-time sample count and duration tracking
- Quality assessment with SNR scoring
- **Live Inference Interface**: Real-time transcription and speaker identification
- **Dynamic Audio Processing**: Browser sample rate detection and adaptive processing
- **Extended Buffer Management**: 120-second audio retention for improved accuracy

### 🚧 Future Enhancements
- Spectrogram visualization
- Batch processing interface
- Advanced filtering and search

## API Integration

The application communicates with the Python speaker recognition backend through these endpoints:

- `GET /health` - Health check
- `GET /speakers` - List enrolled speakers with metrics
- `POST /enroll/upload` - Single file enrollment
- `POST /enroll/batch` - Batch file enrollment (new speakers)
- `POST /enroll/append` - Append audio to existing speakers
- `POST /diarize-and-identify` - Speaker identification
- `DELETE /speakers/{id}` - Delete speaker
- `GET /users` - List users
- `POST /users` - Create or get user

## Audio Processing

The React app handles audio processing using the Web Audio API:

- File upload and validation
- MD5 hash calculation for file identification
- Audio decoding and analysis
- Waveform visualization with downsampling
- Speech segment detection using energy-based VAD
- Audio export in WAV format

## Data Persistence

- **User data**: Stored in localStorage as fallback
- **Annotations**: Stored locally by file hash for consistency
- **Speaker data**: Retrieved from backend API
- **Audio files**: Processed in-browser, exported as needed

## Browser Compatibility

- Chrome/Edge 88+
- Firefox 87+
- Safari 14+

Requires modern browser with Web Audio API support for audio processing features.
