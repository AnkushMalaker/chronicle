# Qwen2.5-Omni Experimental Speaker Recognition

An experimental speaker recognition and diarization system using **Qwen2.5-Omni** for enrollment-by-context (few-shot) speaker identification. This system is designed for **closed-set family member recognition** and produces transcripts with **real names** and **overlap-tolerant diarization**.

## 🎯 Key Features

- **No Training Required**: Uses few-shot learning with 2-3 reference clips per person
- **Real Names**: Direct output with actual names instead of "Speaker 1", "Speaker 2"
- **Overlap Handling**: Supports simultaneous speech detection and transcription
- **Closed-Set Optimization**: Perfect for family/group member identification
- **Context Preservation**: Consistent speaker mappings across multiple audio clips
- **Automatic Chunking**: Handles long audio files with overlap management

## 🆚 Comparison with Traditional Approach

| Feature | Traditional (PyAnnote) | Qwen2.5-Omni Experimental |
|---------|----------------------|--------------------------|
| Training | Requires large datasets | No training (few-shot) |
| Speaker Output | Speaker IDs (0, 1, 2) | Real names (Mom, Dad, etc.) |
| Overlap Handling | Limited | Native support |
| Setup Complexity | High (embeddings, FAISS) | Simple (reference clips) |
| Context Memory | None | Preserves across clips |
| Model Size | Multiple models | Single unified model |

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- 8GB+ VRAM for 7B model (or 4GB+ for 3B model)
- CUDA-compatible GPU (optional, CPU supported)

### 1. Installation

```bash
# Clone or navigate to the experimental directory
cd extras/speaker-omni-experimental

# Install dependencies with UV (recommended)
uv sync

# Install dependencies with pip (alternative)
pip install -r requirements.txt
```

#### GPU Acceleration Setup

**TODO**: Add GPU dependencies using UV for CUDA acceleration:

```bash
# For GPU acceleration (TODO: add to project)
uv add torch[cuda] --group gpu
uv add flash-attn --group gpu
uv add accelerate --group gpu

# Alternative: Manual PyTorch CUDA installation
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

For CPU-only usage, the default dependencies are sufficient.

### 2. Data Preparation Options

You have two options for preparing speaker data:

#### Option A: Automatic Data Preparation from YouTube

**NEW**: Use the automated data preparation script to extract speaker segments from YouTube videos:

```bash
# Set your Deepgram API key
export DEEPGRAM_API_KEY="your-deepgram-key"

# Process a YouTube video (family conversation, podcast, etc.)
uv run python data_preparation.py process --url "https://youtube.com/watch?v=..." --output-dir data/

# This will:
# 1. Download high-quality audio from YouTube
# 2. Transcribe with speaker diarization using Deepgram
# 3. Extract and analyze speaker segments
# 4. Create reference clips for each detected speaker
# 5. Generate a config.yaml file automatically
```

The script creates this structure:
```
data/
├── raw_audio/           # Downloaded YouTube audio
├── segments/            # Individual speaker segments
├── reference_clips/     # Best segments organized by speaker
│   ├── speaker_0/       # Auto-detected speakers
│   ├── speaker_1/
│   └── speaker_2/
├── transcripts/         # Deepgram JSON responses
└── metadata/           # Processing metadata
```

#### Option B: Manual Reference Clips

Create 5-15 second audio clips for each family member manually:

```bash
# Create reference clips directory structure
mkdir -p reference_clips

# Add your reference clips:
# reference_clips/flowerin_01.wav
# reference_clips/flowerin_02.wav
# reference_clips/brother_01.wav
# reference_clips/dad_01.wav
# etc.
```

**Quality Guidelines for Reference Clips:**
- **Duration**: 5-15 seconds each
- **Quantity**: 2-3 clips per person
- **Quality**: Clear speech, minimal background noise
- **Content**: Natural conversation (avoid reading)
- **Variety**: Different emotions/speaking styles
- **Format**: WAV, MP3, FLAC (16kHz+ recommended)

### 3. Configure Speakers

#### If using automatic data preparation:
The script generates a `config.yaml` with placeholder names. Edit it to use real names:

```yaml
speakers:
  # Replace these placeholder names with real family member names
  Flowerin:  # was "Alice"
    - "data/reference_clips/speaker_0/speaker_0_ref_01.wav"
    - "data/reference_clips/speaker_0/speaker_0_ref_02.wav"

  Brother:   # was "Bob"
    - "data/reference_clips/speaker_1/speaker_1_ref_01.wav"
    - "data/reference_clips/speaker_1/speaker_1_ref_02.wav"
```

#### If using manual reference clips:
Create `config.yaml` manually:

```yaml
speakers:
  Flowerin:
    - "reference_clips/flowerin_01.wav"
    - "reference_clips/flowerin_02.wav"

  Brother:
    - "reference_clips/brother_01.wav"
    - "reference_clips/brother_02.wav"

  Dad:
    - "reference_clips/dad_01.wav"
    - "reference_clips/dad_02.wav"
```

### 4. Test the System

```bash
# Enroll speakers (loads model and validates references)
uv run python qwen_speaker_diarizer.py enroll --config config.yaml

# Transcribe a single audio file
uv run python qwen_speaker_diarizer.py transcribe --audio test_audio/family_conversation.wav --config config.yaml

# Batch process multiple files
uv run python qwen_speaker_diarizer.py batch --input-dir test_audio/ --config config.yaml --output-dir output/
```

## 📋 Usage Examples

## 📋 Data Preparation Usage Examples

### Process YouTube Video for Family Data

```bash
# Process family conversation from YouTube
uv run python data_preparation.py process \
    --url "https://youtube.com/watch?v=family_video" \
    --output-dir family_data/ \
    --deepgram-key $DEEPGRAM_API_KEY

# Extract reference clips from existing transcript
uv run python data_preparation.py extract-refs \
    --transcript family_data/transcripts/video_transcript.json \
    --audio family_data/raw_audio/video_processed.wav \
    --output-dir family_data/

# Generate config from existing reference clips
uv run python data_preparation.py generate-config \
    --data-dir family_data/ \
    --output family_config.yaml
```

## 🎤 Speaker Recognition Usage Examples

### Single File Transcription

```bash
# Basic transcription
uv run python qwen_speaker_diarizer.py transcribe \
    --audio "family_dinner.wav" \
    --config config.yaml

# Save results to file
uv run python qwen_speaker_diarizer.py transcribe \
    --audio "family_dinner.wav" \
    --config config.yaml \
    --output "results/dinner_transcript.json"

# Use lighter 3B model for faster processing
uv run python qwen_speaker_diarizer.py transcribe \
    --audio "family_dinner.wav" \
    --config config.yaml \
    --model "Qwen/Qwen2.5-Omni-3B"

# Disable automatic chunking (for short audio)
uv run python qwen_speaker_diarizer.py transcribe \
    --audio "short_clip.wav" \
    --config config.yaml \
    --no-chunk
```

### Batch Processing

```bash
# Process all audio files in a directory
uv run python qwen_speaker_diarizer.py batch \
    --input-dir "recordings/" \
    --config config.yaml \
    --output-dir "transcripts/"
```

### Speaker Enrollment Only

```bash
# Just enroll speakers without transcribing
uv run python qwen_speaker_diarizer.py enroll --config config.yaml
```

## 📊 Output Format

The system outputs structured transcripts with timing and speaker information:

```
<SEG t_start=0.00 t_end=3.20 speaker=Flowerin overlap=false> Hey everyone, how was your day? </SEG>
<SEG t_start=2.80 t_end=4.50 speaker=Brother overlap=true> It was good! </SEG>
<SEG t_start=4.10 t_end=7.30 speaker=Dad overlap=false> I had an interesting meeting at work today. </SEG>
<SEG t_start=7.00 t_end=8.90 speaker=Mom overlap=true> Tell us about it! </SEG>
<SEG t_start=8.50 t_end=12.10 speaker=Unknown speaker 1 overlap=false> Hello, I'm visiting. </SEG>
```

**Output Fields:**
- `t_start/t_end`: Approximate start/end times in seconds
- `speaker`: Real name (for enrolled) or "Unknown speaker N" (for unrecognized)
- `overlap`: Boolean indicating if multiple people are speaking
- `transcript`: The spoken text

## ⚙️ Configuration Options

### Model Selection

```yaml
model:
  model_id: "Qwen/Qwen2.5-Omni-7B"  # or "Qwen/Qwen2.5-Omni-3B"
  device_map: "auto"  # "auto", "cpu", "cuda:0", etc.
```

**Model Comparison:**
- **7B Model**: Better accuracy, requires ~8GB VRAM
- **3B Model**: Faster processing, requires ~4GB VRAM

### Audio Processing

```yaml
audio:
  chunk_duration: 30      # Maximum duration per chunk
  overlap_duration: 5     # Overlap between chunks
  auto_chunk: true        # Enable automatic chunking
```

### Advanced Settings

```yaml
advanced:
  max_new_tokens: 1024    # Max tokens for transcript
  temperature: 0.0        # Generation temperature (0.0 = deterministic)
  enable_audio_output: false  # Keep false to save VRAM
```

## 🔧 Troubleshooting

### Common Issues

**"Model not found" Error:**
```bash
# Ensure you have access to Qwen2.5-Omni models
# You may need to accept the license on Hugging Face
```

**Out of Memory (OOM) Error:**
```bash
# Try the smaller 3B model
python qwen_speaker_diarizer.py transcribe --model "Qwen/Qwen2.5-Omni-3B" ...

# Or use CPU inference
# Edit config.yaml: device_map: "cpu"
```

**Reference clips not found:**
```bash
# Check file paths in config.yaml
# Ensure audio files exist and are readable
# Supported formats: WAV, MP3, FLAC, M4A, OGG
```

**Poor recognition accuracy:**
```bash
# Add more reference clips (2-3 per person minimum)
# Ensure reference clips are high quality
# Use clips from different conversations/contexts
# Check that names in config match exactly
```

### Performance Optimization

**For GPU acceleration:**
```bash
# Install CUDA-optimized PyTorch
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

# Monitor GPU usage
nvidia-smi
```

**For CPU inference:**
```bash
# Use CPU device map in config.yaml
device_map: "cpu"

# Consider using the 3B model for better performance
```

## 📈 Performance Benchmarks

Approximate processing times (7B model on RTX 4090):

| Audio Duration | Processing Time | Memory Usage |
|---------------|----------------|--------------|
| 30 seconds    | ~15 seconds    | ~8GB VRAM   |
| 2 minutes     | ~45 seconds    | ~8GB VRAM   |
| 10 minutes    | ~3.5 minutes   | ~8GB VRAM   |

*Note: First run includes model loading time (~30-60 seconds)*

## 🔮 Integration Path

This experimental system can be integrated with the existing Chronicle backend:

1. **Standalone Testing**: Use this directory for initial family testing
2. **API Wrapper**: Create FastAPI endpoint similar to traditional speaker service
3. **Backend Integration**: Replace/supplement PyAnnote in advanced backend
4. **Gradual Migration**: A/B test against traditional system
5. **Full Deployment**: Replace traditional system if results are superior

### Potential Integration Points

- `backend/src/backend/processors.py`
- `extras/speaker-recognition/speaker_service.py` (replacement)
- New microservice alongside existing speaker recognition

## 🔗 Related Files

- `../speaker-recognition/`: Traditional PyAnnote-based system
- `../../backend/`: Main Chronicle backend
- `../../extras/test-audios/`: Sample audio files for testing

## 📝 Development Notes

- **Model Loading**: First run downloads ~14GB model (7B) or ~6GB (3B)
- **Caching**: Models are cached locally by Transformers
- **Memory Management**: Audio chunking prevents OOM on long files
- **Error Handling**: Graceful fallback and cleanup on failures
- **Logging**: Comprehensive logging for debugging and monitoring

## 🤝 Contributing

This is an experimental system. Feedback and improvements welcome:

1. Test with your family audio recordings
2. Report accuracy compared to traditional system
3. Suggest configuration improvements
4. Performance optimization ideas

## 📄 License

Part of the Chronicle project. See main repository license.
