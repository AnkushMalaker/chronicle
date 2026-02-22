# Event Detection - Whisper LoRA Adapter

**🟢 STATUS: TRAINING/EXPORT WORKFLOW (User-Loop → Export → Training)**

This folder contains **training/export utilities** for Whisper + LoRA event detection. It integrates with the **Chronicle user-loop** for continuous data collection and training.

---

## 📋 Overview

This system uses a **LoRA (Low-Rank Adaptation)** adapter on top of Whisper's Large V3 model to detect specific custom events (sounds, keywords, phrases) in audio.

### Workflow:

```
Backend Anomaly Scan Job (sets maybe_anomaly: true)
        │
        ▼
User-Loop Popup (Review Anomalies)
        │
        ├──► Swipe Right → Accept/Verify
        │       │
        │       ▼
        │  MongoDB: maybe_anomaly = "verified"
        │
        └──► Swipe Left → Reject/Stash
                │
                ▼
         MongoDB: training_stash collection
                │
                ▼
        Export: user_loop_feedback.jsonl
                │
                ▼
        Train: LoRA adapter
```

---

## 📁 Files

| File | Purpose | Status |
|-------|----------|--------|
| `export_from_mongo.py` | Export MongoDB `training_stash` to JSONL for training | ✅ Active (Bridge) |
| `train.py` | Fine-tune Whisper with LoRA adapter | ✅ Active |
| `requirements.txt` | Python dependencies | ✅ Active |

Anomaly flagging (setting `maybe_anomaly: true` in MongoDB) is handled by the backend script `backends/advanced/src/advanced_omi_backend/scripts/run_anomaly_detection.py`.

---

## 🚀 Production Workflow

### Step 1: Data Collection (User-Loop)

**Users interact with user-loop popup:**

1. **Frontend shows popup** when conversations have `maybe_anomaly: true`
2. **User reviews transcript** and audio
3. **Swipe Left** → Reject (stashes for training)
4. **Swipe Right** → Accept (marks as verified, `maybe_anomaly: "verified"`)

**MongoDB Collections:**

```javascript
// conversations - User-Loop reviews these
{
  "conversation_id": "1a43e276-...",
  "transcript_versions": [{
    "version_id": "c9c392d9-...",
    "maybe_anomaly": true,  // Triggers popup
    "transcript": "The stale smell of old beer..."
  }]
}

// training_stash - User-Loop saves rejected items here
{
  "_id": ObjectId("..."),
  "version_id": "c9c392d9-...",
  "conversation_id": "1a43e276-...",
  "transcript": "The stale smell of old beer...",
  "reason": "False positive",
  "timestamp": 1738254720.123,
  "audio_chunks": [...],
  "metadata": {"word_count": 43}
}
```

---

### Step 2: Export Training Data (Bridge)

**Export MongoDB `training_stash` collection to JSONL format:**

```bash
uv run python export_from_mongo.py \
  --output user_loop_feedback.jsonl \
  --min_samples 10
```

**Output (`user_loop_feedback.jsonl`):**
```json
{"audio": "/data/audio/1a43e276-....wav", "text": "The stale smell of old beer...", "type": "positive", "timestamp": "2024-01-30T10:00:00Z"}
{"audio": "/data/audio/another-id.wav", "text": "Transcription with <event>", "type": "positive", "timestamp": "2024-01-30T10:05:00Z"}
```

**Arguments:**
- `--output`: Output JSONL file path (default: `user_loop_feedback.jsonl`)
- `--mongo_uri`: MongoDB connection (default: `mongodb://localhost:27017`)
- `--db_name`: Database name (default: `chronicle`)
- `--min_samples`: Minimum samples to export (default: 0)

**Schema Mapping:**
```python
# MongoDB → Training JSONL
{
    "audio": f"/data/audio/{entry['conversation_id']}.wav",
    "text": entry["transcript"],
    "timestamp": entry.get("timestamp"),
    "type": "positive"  # All user-loop rejections = positive for training
}
```

---

### Step 3: Train LoRA Adapter

**Fine-tune Whisper with exported user-loop data:**

```bash
uv run python train.py \
  --train_manifest user_loop_feedback.jsonl \
  --output_dir ./sneeze_adapter \
  --base_model unsloth/whisper-large-v3 \
  --source_tag "<event>" \
  --target_token "EVENT_DETECTED"
```

**Training Parameters:**
- `--train_manifest`: JSONL file from export (default: `train.jsonl`)
- `--output_dir`: Directory to save adapter (default: `event_lora_adapter_unsloth`)
- `--base_model`: Whisper model ID (default: `unsloth/whisper-large-v3`)
- `--source_tag`: Tag in text to replace (default: `<event>`)
- `--target_token`: Token to emit for event (default: `EVENT_DETECTED`)

**Output:**
```bash
./sneeze_adapter/
  ├── adapter_config.json
  ├── adapter_model.safetensors
  └── README.md
```

---

### Step 4: Flag New Anomalies (Backend Job)

The backend provides a MongoDB scan job that sets `transcript_versions.$.maybe_anomaly = True` for transcripts that haven't been reviewed yet.

From `backends/advanced/`:

```bash
uv run python src/advanced_omi_backend/scripts/run_anomaly_detection.py
```

Notes:
- Configure MongoDB via `MONGODB_URI` (defaults to `mongodb://localhost:27017`).
- This script is currently a placeholder implementation (it marks unflagged transcripts as anomalies).

---

## 🔄 Full Cycle Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    USER INTERACTION PHASE                 │
│  Frontend shows popup when maybe_anomaly: true          │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │ Swipe Actions         │
        │ Left=Reject Right=Accept
   ┌────┴────┐         ┌────┴────┐
   │ Swipe   │         │ Swipe   │
   │ Left    │         │ Right   │
   ▼         ▼         ▼         ▼
 Reject    Reject   Accept   Accept
 (stash)   (stash)  (verify) (verify)
   │         │         │         │
   ▼         ▼         │         │
 MongoDB:   MongoDB:   │         │
 training_  training_ ▼         ▼
 stash      stash   maybe_   maybe_
                      anomaly  anomaly
                      ="verified"
   │         │
   ▼         ▼
 More Data in
 training_stash
                   │
                   ▼
         ┌────────────────────┐
         │ EXPORT PHASE      │
         │ export_from_mongo │
         │   .py            │
         └────────┬─────────┘
                  │
                  ▼
         ┌────────────────────┐
         │ TRAINING PHASE    │
         │   train.py        │
         └────────┬─────────┘
                  │
                  ▼
         ┌────────────────────┐
         │  ./sneeze_      │
         │    adapter/        │
         └────────┬─────────┘
                  │
                  ▼
          ┌──────────────────────────────┐
          │ BACKEND ANOMALY SCAN (JOB)   │
          │ run_anomaly_detection.py     │
          │ sets maybe_anomaly: true     │
          └─────────────┬────────────────┘
                        │
                        ▼
          ┌────────────────────┐
          │   User Popup      │
          │   (Round 2)       │
          └────────────────────┘
                   │
                   └──► Back to start!
```

---

## 📊 Training Data Format

The training JSONL file (`user_loop_feedback.jsonl`) uses this schema:

```json
{
  "audio": "/path/to/audio.wav",
  "text": "Transcription with <event> tag or just normal speech",
  "timestamp": "2024-01-30T10:00:00Z",
  "type": "positive"
}
```

**Fields:**
- `audio`: Path to audio file for training
- `text`: Ground truth transcription (from user-loop transcript)
- `timestamp`: When sample was added (from MongoDB)
- `type`: Always `"positive"` for user-loop data (rejections = positive training samples)

---

## 🧠 Training Details

### LoRA Configuration

- **Base Model**: Whisper Large V3 (unsloth)
- **Adapter Type**: LoRA (Low-Rank Adaptation)
- **Parameters**:
  - `r`: Rank (8-32 recommended)
  - `lora_alpha`: Scaling factor (16-64)
  - `target_modules`: `["q_proj", "v_proj"]`
  - `dtype`: `float16` for CUDA, `float32` for CPU

### Training Process

1. Load base model (Whisper Large V3)
2. Load training data (audio + transcriptions)
3. Fine-tune adapter layers only
4. Validate on test set
5. Save adapter weights to output directory

---

## 🎯 Production Deployment

### Automated Workflow

**Setup Cron Jobs for continuous improvement:**

```bash
# crontab -e

# Export training data daily at 2 AM
0 2 * * * cd /path/to/backends/advanced/event-detection && uv run python export_from_mongo.py --min_samples 50

# Retrain adapter weekly on Sunday at 3 AM
0 3 * * 0 cd /path/to/backends/advanced/event-detection && uv run python train.py --train_manifest user_loop_feedback.jsonl
```

### Adapter Versioning

Store versioned adapters for A/B testing and rollback:

```bash
./adapters/
  ├── sneeze_v1/    # Initial training
  ├── sneeze_v2/    # After 100 samples
  ├── sneeze_v3/    # After 500 samples
  └── sneeze_latest/  # Symlink to current
```

### Monitoring

Track metrics to improve detection:

- **False Positive Rate**: Swipe left (reject) / Total popups
- **True Positive Rate**: Swipe right (accept) / Total swipes right
- **Detection Accuracy**: Correct detections / Total samples

---

## 🐛 Troubleshooting

### Issue: "No entries found in training_stash"

**Symptoms:**
```
❌ No entries found in training_stash collection
💡 Tip: Swipe left on user-loop popup to add samples (reject = stash)
```

**Solutions:**
1. Verify user-loop popup is working
2. Swipe left on some anomalies to add to training_stash
3. Check MongoDB connection
4. Lower `--min_samples` threshold

---

### Issue: "Adapter output directory missing"

**Symptoms:**
```
Expected adapter directory not found: ./sneeze_adapter
```

**Solutions:**
1. Verify `--output_dir` matches where you expect the adapter to be saved
2. Check if train.py completed successfully
3. Ensure the output directory exists and is writable

---

### Issue: "No audio files found" (in export)

**Symptoms:**
```
Exported 0 entries to user_loop_feedback.jsonl
   Has audio chunks: 0/10
```

**Solutions:**
1. Verify conversations have audio in MongoDB
2. Check audio_chunks collection
3. Ensure audio was uploaded correctly

---

### Issue: CUDA out of memory

**Symptoms:**
```
RuntimeError: CUDA out of memory
```

**Solutions:**
1. Reduce `--batch_size` (try 2 or 1)
2. Use CPU with `torch_dtype=torch.float32`
3. Use smaller base model (Whisper Base instead of Large)

---

### Issue: Poor detection accuracy

**Symptoms:**
- High false positive rate
- Misses obvious events
- Random detections

**Solutions:**
1. **More data**: Need at least 100-500 samples
2. **Better labels**: Review user-loop feedback for accuracy
3. **Retrain**: Train with more epochs
4. **Adjust trigger token**: Check if token appears in training data

---

## 📚 Dependencies

Install required packages:

```bash
pip install -r requirements.txt
```

**Key Dependencies:**
- `unsloth`: Optimized Whisper model
- `transformers`: Hugging Face model library
- `peft`: LoRA adapters
- `torch`: Deep learning framework
- `librosa`: Audio processing
- `datasets`: Training data utilities

**System Requirements:**
- Python 3.8+
- CUDA-capable GPU (recommended) or 16GB+ RAM for CPU

---

## 🔗 Integration with Backend

### Current State

**Frontend:**
```typescript
// UserLoopModal.tsx
const checkAnomaly = async () => {
  // TODO: Replace with actual algorithm
  const shouldShow = true  // Always shows popup
}
```

**Backend:**
```python
# user_loop_routes.py
- ✅ GET /api/user-loop/events (returns anomalies)
- ✅ POST /api/user-loop/accept (verifies)
- ✅ POST /api/user-loop/reject (stashes to training)
- ✅ Anomaly scan job: src/advanced_omi_backend/scripts/run_anomaly_detection.py (sets maybe_anomaly: true)
```

### Future Integration

To replace the placeholder scan with **model-based anomaly detection**:

1. Train an adapter in this folder (`train.py`) and version it.
2. Load the adapter in the backend scan job and use inference to decide whether to set `maybe_anomaly: true`.
3. Ensure the UI only opens the user-loop popup when `/api/user-loop/events` returns events.

---

## 📊 Workflow Summary

| Phase | Component | Command |
|--------|-----------|----------|
| **1. Collection** | User-Loop Popup | User swipes left to reject (stash) / right to accept |
| **2. Storage** | MongoDB | Saves to `training_stash` collection |
| **3. Export** | export_from_mongo.py | `uv run python export_from_mongo.py --min_samples 10` |
| **4. Training** | train.py | `uv run python train.py --train_manifest user_loop_feedback.jsonl --output_dir ./sneeze_adapter` |
| **5. Flagging** | Backend job | `cd .. && uv run python src/advanced_omi_backend/scripts/run_anomaly_detection.py` |
| **6. Deployment** | Backend | (Future) Use trained adapter inside the scan job |

---

## 🤝 Contributing

To improve event detection:

1. **Collect More Data**: Swipe left on user-loop popup to reject (stash) samples
2. **Review Labels**: Check training data quality
3. **Retrain Often**: Update adapter weekly with new data
4. **A/B Test**: Compare new vs old adapters
5. **Monitor Metrics**: Track false positive/negative rates

---

**Last Updated**: January 30, 2026
**Status**: 🟢 Training/Export Workflow (User-Loop → Export → Training) ✅
**Backend Integration**: 🟡 Partial (flagging job exists; model-based detection pending)
