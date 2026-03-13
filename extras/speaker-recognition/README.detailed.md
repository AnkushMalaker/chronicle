# Speaker Recognition System - Detailed Documentation

## Table of Contents
1. [Key Concepts](#key-concepts)
2. [System Architecture](#system-architecture)
3. [Getting Started](#getting-started)
4. [Web UI Guide](#web-ui-guide)
5. [API Reference](#api-reference)
6. [Data Export](#data-export)
7. [Implementation Status](#implementation-status)

## Key Concepts

### Speech Recognition vs Speaker Recognition

**Speech Recognition** (Speech-to-Text):
- **Purpose**: Converts spoken audio into written text
- **Input**: Audio waveform containing speech
- **Output**: Transcribed text of what was said
- **Example**: Audio of someone saying "Hello world" � Text: "Hello world"
- **Use cases**: Virtual assistants, dictation, subtitles

**Speaker Recognition** (Voice Identification):
- **Purpose**: Identifies who is speaking based on voice characteristics
- **Input**: Audio waveform containing speech
- **Output**: Speaker identity (name or ID)
- **Example**: Audio clip � "This is John Smith speaking"
- **Use cases**: Security systems, personalization, forensics

### Speaker Diarization

**What is Speaker Diarization?**
Speaker diarization is the process of partitioning an audio stream into segments according to the speaker identity. It answers the question "who spoke when?" without necessarily knowing who the speakers are.

**Key Points**:
- Does NOT transcribe speech (only identifies different speakers)
- Does NOT identify who the speakers are (just labels them as Speaker_0, Speaker_1, etc.)
- Useful for meeting transcription, interview analysis, call center analytics

**Example Output**:
```
0.0s - 5.2s: Speaker_0
5.2s - 8.7s: Speaker_1
8.7s - 12.1s: Speaker_0
12.1s - 15.0s: Speaker_2
```

### How PyAnnote Audio Works

PyAnnote Audio is the library we use for speaker diarization. Here's how it processes audio:

1. **Voice Activity Detection (VAD)**
   - Identifies regions containing speech vs silence/noise
   - Filters out non-speech segments

2. **Speaker Embedding Extraction**
   - Converts speech segments into numerical vectors (embeddings)
   - Each vector represents the unique characteristics of a voice

3. **Clustering**
   - Groups similar embeddings together
   - Each cluster represents a different speaker

4. **Temporal Labeling**
   - Assigns speaker labels to time segments
   - Produces the final diarization timeline

**Important**: PyAnnote doesn't know the actual identity of speakers - it just knows they're different voices.

### Speaker Enrollment

The process of registering a known speaker in the system:

1. **Audio Collection**
   - Record or upload audio sample (recommended: 2+ minutes)
   - Ensure good quality (low noise, clear speech)

2. **Embedding Extraction**
   - Convert voice to numerical representation (512-dimensional vector)
   - This embedding captures unique voice characteristics

3. **Storage**
   - Save embedding with speaker ID and name
   - Store in FAISS index for fast similarity search

4. **Quality Assessment**
   - Calculate SNR (Signal-to-Noise Ratio)
   - Measure speech vs silence ratio
   - Assign quality score (0.0 to 1.0)

### Quality Score Calculation

The enrollment quality score helps ensure reliable speaker recognition:

**Components**:
1. **Duration Score** (40% weight)
   - Optimal: 30-120 seconds of actual speech
   - Too short: Not enough voice variation
   - Too long: Diminishing returns

2. **SNR Score** (40% weight)
   - Measures audio clarity
   - >20 dB: Excellent
   - 15-20 dB: Good
   - 10-15 dB: Acceptable
   - <10 dB: Poor

3. **Speech Ratio** (20% weight)
   - Percentage of audio containing speech
   - >70%: Excellent
   - 50-70%: Good
   - <50%: May need more samples

**Formula**: `quality = 0.4 � duration_score + 0.4 � snr_score + 0.2 � speech_ratio`

## System Architecture

### Components

1. **FastAPI Backend** (`speaker_service.py`)
   - Handles speaker recognition operations
   - Manages PyAnnote and SpeechBrain models
   - Provides RESTful API endpoints

2. **Streamlit Web UI** (`web_ui.py`)
   - User-friendly interface for all operations
   - Real-time audio visualization
   - Data management and export

3. **SQLite Database** (`data/speakers.db`)
   - Stores user information
   - Tracks speaker profiles
   - Manages annotations and sessions
   - Maintains export history

4. **File Storage** (`data/audio_cache/`)
   - Temporary audio file storage
   - Processed segment cache
   - Export file staging

### Data Flow

1. **Audio Input** � Web UI or API
2. **Processing** � FastAPI backend
3. **Storage** � SQLite + File System
4. **Retrieval** � API queries
5. **Export** � Zip generation � Download

## Getting Started

### Prerequisites

- Python 3.10+
- NVIDIA GPU (recommended) or CPU
- Hugging Face account (for model access)
- 8GB+ RAM
- 10GB+ disk space
- Docker and Docker Compose

### Installation

1. **Set up environment variables**:
   ```bash
   export HF_TOKEN="your_huggingface_token"
   export SIMILARITY_THRESHOLD="0.85"
   ```

2. **Install dependencies**:
   ```bash
   cd extras/speaker-recognition
   uv sync  # Or pip install -r requirements.txt
   ```

3. **Run with Docker (Recommended)**:
   ```bash
   docker-compose up --build
   ```

   This starts:
   - Speaker service on http://localhost:8001
   - Web UI on http://localhost:8501

4. **Or run locally**:
   ```bash
   # Terminal 1 - Start speaker service
   uv run python speaker_service.py

   # Terminal 2 - Start web UI
   uv run streamlit run web_ui.py
   ```

### Quick Start

1. **Access the Web UI**: Open http://localhost:8501 in your browser
2. **Create/Select User**: Use the sidebar to create or select your username
3. **Upload Audio**: Go to "Audio Viewer" page and upload an audio file
4. **Annotate Segments**: Use "Annotation" page to label speaker segments
5. **Enroll Speakers**: Use "Enrollment" page to register speakers in the system
6. **Manage & Export**: Use "Speakers" page to manage enrolled speakers and export data
7. **View Analytics**: Check "Analytics" page for system performance insights

## Web UI Guide

### 1. Audio Viewer (Page 1)

**Purpose**: Upload and visualize audio files

**Features**:
- ✅ **File Upload**: Support for WAV, FLAC, MP3, M4A, OGG formats
- ✅ **Waveform Visualization**: Interactive Plotly-based waveform with zoom/pan
- ✅ **Audio Information**: Duration, sample rate, channels, format display
- ✅ **Quality Metrics**: Real-time SNR, speech ratio, and speech duration calculation
- ✅ **Spectrogram Display**: Optional frequency analysis view
- ✅ **Segment Selection**: Manual time input controls for precise segment selection
- ✅ **Audio Playback**: Built-in player for full audio and selected segments
- ✅ **Speech Detection**: Automatic detection of speech vs silence regions
- ✅ **Export Options**: Download segments, full audio, or metadata as JSON

**Usage**:
1. **Upload Audio**: Use the file uploader to select your audio file
2. **View Information**: Check duration, quality metrics, and file details
3. **Explore Waveform**: Use the interactive plot to visualize your audio
4. **Select Segments**: Use manual time input controls for precise segment boundaries
5. **Play Audio**: Listen to full audio or selected segments
6. **Export Data**: Download segments for further analysis or speaker enrollment

### 2. Annotation Tool (Page 2)

**Purpose**: Label speaker segments in audio files with Deepgram import support

**Features**:
- ✅ **Audio Upload**: Upload audio files for manual annotation
- ✅ **Deepgram JSON Import**: Import Deepgram transcription files with automatic segmentation
- ✅ **Speaker Mapping Interface**: Map Deepgram speaker labels (speaker_0, speaker_1) to enrolled speakers
- ✅ **Bulk Speaker Assignment**: "Apply to all speaker_X" functionality for efficient labeling
- ✅ **Timeline Visualization**: Interactive timeline showing all annotations with color coding
- ✅ **Speaker Assignment**: Dropdown with enrolled speakers + unknown speaker options
- ✅ **Unknown Speaker Support**: Create "Unknown Speaker 1, 2, 3..." or custom labels
- ✅ **Quality Labels**: Mark segments as CORRECT, INCORRECT, or UNCERTAIN
- ✅ **Segment Creation**: Manual time input for precise segment boundaries
- ✅ **Audio Playback**: Play individual segments for verification
- ✅ **Transcription Display**: Show transcript text for each segment (from Deepgram)
- ✅ **Annotation Management**: Edit, delete, and review all annotations
- ✅ **Batch Operations**: Apply labels to multiple segments, filter by quality
- ✅ **Database Persistence**: Save annotations to SQLite database with Deepgram metadata
- ✅ **Export Options**: JSON export for external use

**Usage Workflow**:

**Option A: Manual Annotation**
1. **Upload Audio**: Select the audio file you want to annotate
2. **Create Segments**: Define start/end times for speaker segments
3. **Assign Speakers**: Choose from enrolled speakers or create unknown speakers
4. **Set Quality**: Mark segments as CORRECT, INCORRECT, or UNCERTAIN
5. **Review Timeline**: Use the visual timeline to see all annotations
6. **Save Work**: Persist annotations to database or export as JSON

**Option B: Deepgram Import**
1. **Upload Files**: Upload both audio file and Deepgram JSON transcript
2. **Import Transcript**: System automatically creates segments from Deepgram output
3. **Map Speakers**: Use speaker mapping interface to assign speaker_0, speaker_1 etc. to known speakers
4. **Bulk Assignment**: Apply speaker mappings to all segments with "Apply to all" feature
5. **Review & Edit**: Fine-tune individual segments, add quality labels, edit transcriptions
6. **Save Work**: Persist annotations with Deepgram metadata to database

### 3. Enrollment (Page 3)

**Purpose**: Register new speakers in the system

**Two Modes**:

#### Guided Recording (✅ **Fully Implemented**)
- ✅ **Live Recording**: WebRTC-based real-time audio capture with streamlit-webrtc
- ✅ **Conversation Prompts**: 10 varied prompts to encourage natural speech
- ✅ **Prompt Navigation**: Easy switching between conversation topics
- ✅ **Real-time Quality Feedback**: Live monitoring of SNR, speech ratio, and quality score
- ✅ **Recording Timer**: Progress tracking with 30s minimum and 2-minute target
- ✅ **Audio Preview**: Playback recorded audio before enrollment
- ✅ **Quality Assessment**: Automatic evaluation with enrollment recommendations
- ✅ **WebRTC Integration**: Browser-based microphone access and real-time processing

#### File Upload (✅ **Fully Implemented**)
- ✅ **Multiple File Upload**: Batch processing of audio files
- ✅ **Quality Assessment**: Comprehensive analysis of each file
- ✅ **Quality Scoring**: Duration, SNR, and speech ratio evaluation
- ✅ **Visual Feedback**: Color-coded quality indicators and recommendations
- ✅ **File Selection**: Choose which files to include in enrollment
- ✅ **Waveform Display**: Optional visualization for each uploaded file
- ✅ **Audio Playback**: Preview files before enrollment
- ✅ **Automatic Processing**: Direct integration with speaker service API
- ✅ **Quality Recommendations**: Specific tips for improving audio quality
- ✅ **Enrollment History**: Track all enrollment sessions per speaker

### 4. Speaker Management (Page 4)

**Purpose**: Manage enrolled speakers and export data

**Features**:
- ✅ **Speaker List**: View all enrolled speakers with expandable details
- ✅ **Quality Statistics**: Duration, SNR, speech ratio, and quality scores per speaker
- ✅ **Enrollment History**: Track all enrollment sessions with timestamps and methods
- ✅ **Speaker Details**: View comprehensive statistics and quality trends over time
- ✅ **Speaker Editing**: Update speaker names and notes
- ✅ **Speaker Deletion**: Remove speakers with confirmation dialog
- ✅ **Multi-Speaker Selection**: Select speakers for batch operations
- ✅ **Export Functionality**: Multiple export formats and options
- ✅ **Quality Comparison**: Compare metrics across multiple speakers
- ✅ **Bulk Operations**: Process multiple speakers simultaneously

**Export Options**:
- ✅ **Concatenated Format**: Audio segments combined into files (max 10 minutes each)
- ✅ **Segmented Format**: Each annotation as separate file in organized folders
- ✅ **Audio Formats**: WAV or MP3 output
- ✅ **Metadata Export**: JSON files with timestamps, quality scores, and session info
- ✅ **Annotations Export**: Include annotation labels, confidence, and notes
- ✅ **Folder Structure**: Organized as `./exported_data/speaker-name/audio001.wav`
- ✅ **ZIP Download**: Automatic packaging for easy download

### 5. Analytics (Page 5)

**Purpose**: System performance and usage analytics

**Features**:
- ✅ **System Overview**: Total speakers, annotations, accuracy rates, and session counts
- ✅ **Quality Distribution**: Pie chart showing CORRECT/INCORRECT/UNCERTAIN annotation breakdown
- ✅ **Speaker Comparison**: Multi-metric comparison of quality scores across speakers
- ✅ **Quality Analysis**: Detailed breakdown of enrollment quality by speaker
- ✅ **Activity Trends**: Timeline charts showing enrollment activity over time
- ✅ **Recent Activity**: Summary of recent enrollment sessions and annotations
- ✅ **System Recommendations**: Personalized tips for improving system usage
- ✅ **Best Practices**: Guidelines for optimal recognition accuracy and workflow

**Metrics Displayed**:
- ✅ **Enrollment Quality**: Duration, SNR, speech ratio, and overall quality scores
- ✅ **Recognition Accuracy**: Annotation correctness rates and confidence levels
- ✅ **Usage Patterns**: Daily activity, session counts, and speaker engagement
- ✅ **Quality Trends**: Time-series analysis of enrollment quality improvements

## API Reference

### Authentication
No authentication required for standalone deployment.

### Endpoints

#### Existing Endpoints

**POST /enroll/upload**
- Enroll a new speaker with audio file
- Multipart form data with audio file
- Returns: Speaker ID and enrollment status

**POST /identify/upload**
- Identify speaker in audio file
- Returns: Speaker ID, name, and confidence score

**POST /diarize/upload**
- Perform speaker diarization
- Returns: Timeline of speaker segments

**GET /speakers**
- List all enrolled speakers
- Returns: Array of speaker objects

**DELETE /speakers/{speaker_id}**
- Remove an enrolled speaker
- Returns: Deletion confirmation

#### New Endpoints (To be implemented)

**POST /annotations/save**
- [To be implemented] Save annotation batch

**GET /annotations/{audio_id}**
- [To be implemented] Get annotations for audio file

**POST /enroll/batch**
- [To be implemented] Batch speaker enrollment

**GET /enrollment/quality/{speaker_id}**
- [To be implemented] Get detailed quality metrics

**POST /audio/segment**
- [To be implemented] Extract specific audio segment

**GET /audio/waveform/{file_id}**
- [To be implemented] Get waveform data for visualization

**POST /speakers/merge**
- [To be implemented] Merge multiple speaker profiles

**GET /speakers/similarity**
- [To be implemented] Get speaker similarity matrix

## Data Export

### Export Options

#### Single Speaker Export

**Concatenated Format**:
- All segments joined into single files
- Maximum 10 minutes per file
- Automatic splitting for longer content
- WAV format, 16kHz, mono

**Segmented Format**:
```
speaker-{name}/
   audio0001.wav
   audio0002.wav
   audio0003.wav
   metadata.json
```

#### Bulk Export

**All Speakers**:
- [To be implemented] Export all speakers for a user
- [To be implemented] Organized folder structure
- [To be implemented] Includes metadata and annotations

**Filtered Export**:
- [To be implemented] Export by quality threshold
- [To be implemented] Export by date range
- [To be implemented] Export specific users only

### Export Formats

- **Audio**: WAV (default), MP3 (compressed)
- **Metadata**: JSON with timestamps and quality scores
- **Annotations**: CSV or JSON format
- **Embeddings**: NumPy NPZ format

## Implementation Status

###  Completed
- Basic FastAPI speaker service
- PyAnnote diarization integration
- SpeechBrain embedding extraction
- FAISS similarity search
- Simple enrollment and identification

### =� In Progress
- Streamlit web UI framework
- SQLite database schema
- User management system

### =� Planned
- Audio visualization components
- Annotation interface
- Guided recording system
- Export functionality
- Analytics dashboard
- Batch processing
- Quality metrics
- Speaker management UI

### =. Future Enhancements
- Real-time streaming enrollment
- Multi-language support
- Speaker verification (1:1 matching)
- Age/gender detection
- Emotion recognition
- Cloud storage integration

## Troubleshooting

### Common Issues

**Model Download Fails**
- Ensure HF_TOKEN is set correctly
- Check internet connection
- Verify Hugging Face account has accepted model agreements

**GPU Not Detected**
- Verify CUDA installation
- Check PyTorch GPU support: `python -c "import torch; print(torch.cuda.is_available())"`
- Fall back to CPU if needed

**Poor Recognition Accuracy**
- Check audio quality (SNR >15 dB recommended)
- Ensure sufficient enrollment data (2+ minutes)
- Verify speakers are distinct (not too similar)
- Consider re-enrollment with better samples

**Export Fails**
- Check disk space availability
- Verify file permissions
- Ensure audio files still exist
- Check export size limits

## Best Practices

### For Enrollment
1. Use quiet environment
2. Speak naturally and continuously
3. Include voice variation (questions, statements)
4. Avoid background music or TV
5. Use consistent microphone distance

### For Annotation
1. Listen to full segment before labeling
2. Use UNCERTAIN when not confident
3. Create new unknown speakers liberally
4. Merge speakers later if needed
5. Save work frequently

### For System Performance
1. Limit concurrent users to CPU cores
2. Use GPU for faster processing
3. Regular database maintenance
4. Clean up old export files
5. Monitor disk space usage
