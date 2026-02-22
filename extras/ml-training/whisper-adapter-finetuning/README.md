# Whisper Sneeze Adapter Training

This project fine-tunes OpenAI's Whisper model to transcribe sneezes in audio/video content using LoRA adapters. The model learns to recognize and transcribe sneezes as the token "SNEEZE" in transcriptions.

## Prerequisites

- Python 3.10+
- CUDA-capable GPU (recommended for training)
- Access to Google Gemini API (for generating transcripts)

## Installation

1. Create a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install torch torchaudio
pip install transformers datasets evaluate
pip install unsloth[colab-new]
pip install librosa soundfile jiwer
pip install tqdm
```

## Workflow

### Step 1: Prepare Your Video

1. Record or obtain a video file containing sneezes (e.g., `girls_sneezing.mp4` download with
   ```
   yt-dlp -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" --merge-output-format mp4 -o "girls_sneezing.mp4" https://youtu.be/36b4248j5UE
   ```

### Step 2: Generate Transcript with Gemini

1. Upload your video to Google Gemini (or use Gemini API)
2. Request a transcript with sneezes marked using the format: `<sneeze>`
3. Generate a JSONL file named `sneeze_data.jsonl` with the following format:

```jsonl
{"start": 0.0, "end": 5.0, "text": "Ugh, I really need to sneeze. Stuck? Yeah, it's right there."}
{"start": 5.0, "end": 11.0, "text": "Close one. <sneeze> Bless you. Thanks."}
{"start": 12.0, "end": 17.0, "text": "Ugh, I can feel it. I really need to sneeze so bad. Go on, let it out."}
```

**Format requirements:**
- Each line is a JSON object
- `start`: Start time in seconds (float)
- `end`: End time in seconds (float)
- `text`: Transcription text with sneezes marked as `<sneeze>`

**Example Gemini prompt:**
```
Please transcribe this video and create a JSONL file where each line contains:
- start: start time in seconds
- end: end time in seconds
- text: the transcription with sneezes marked as <sneeze>

Format as JSONL (one JSON object per line).
```

### Step 3: Prepare Training Data

Run the data preparation script to extract audio chunks and create train/test splits:

```bash
python prepare_sneeze_data.py
```

This script will:
- Extract audio from your video file (`girls_sneezing.mp4`)
- Create audio chunks from the segments in `sneeze_data.jsonl`
- Save chunks to `sneeze_chunks/` directory
- Split data into `train.jsonl` (60%) and `test.jsonl` (40%)

**Requirements:**
- `sneeze_data.jsonl` must exist in the project root
- Video file must be named `girls_sneezing.mp4`

### Step 4: Train the Model

Train the Whisper model with LoRA adapters:

```bash
python train_sneeze.py
```

This will:
- Load the base Whisper Large v3 model
- Apply LoRA adapters (only trains 1-10% of parameters)
- Fine-tune on your sneeze data
- Save the adapter to `sneeze_lora_adapter_unsloth/`

**Training configuration:**
- Model: `unsloth/whisper-large-v3`
- LoRA rank: 64
- Batch size: 1 (with gradient accumulation: 4)
- Max steps: 200
- Learning rate: 1e-4

**Note:** Training requires a GPU with sufficient VRAM. Adjust `load_in_4bit=True` in the script if you have limited memory.

### Step 5: Evaluate the Model

Evaluate the trained model on the test set:

```bash
python evaluate_sneeze_model.py
```

This will:
- Load the base model and merge the LoRA adapter
- Run inference on test samples
- Calculate Word Error Rate (WER)
- Report sneeze detection recall and false positives

## Results

### Training Results

Training was performed on a Tesla T4 GPU with the following configuration:
- **Model**: `unsloth/whisper-large-v3`
- **Trainable Parameters**: 31,457,280 of 1,574,947,840 (2.00%)
- **Training Time**: 12.04 minutes
- **Peak Memory Usage**: 8.896 GB (60.35% of max memory)
- **Training Samples**: 49 samples
- **Test Samples**: 4 samples

**Training Loss Progression:**
| Step | Training Loss | Validation Loss | WER |
|------|---------------|-----------------|-----|
| 20   | 1.646100      | 1.869532        | 50.0% |
| 40   | 0.832500      | 1.004385        | 30.0% |
| 60   | 0.304600      | 0.354044        | 30.0% |
| 80   | 0.067700      | 0.051606        | 0.0% |
| 100  | 0.017600      | 0.162433        | 10.0% |
| 120  | 0.003400      | 0.006127        | 0.0% |
| 140  | 0.002000      | 0.004151        | 0.0% |
| 160  | 0.001400      | 0.003399        | 0.0% |
| 180  | 0.001300      | 0.003005        | 0.0% |
| 200  | 0.001000      | 0.002856        | 0.0% |

**Final Metrics:**
- Final Training Loss: 0.001000
- Final Validation Loss: 0.002856
- Final Validation WER: 0.0%

### Evaluation Results

Evaluation was performed on 10 test samples (4 containing sneezes):

**Overall Performance:**
- **Word Error Rate (WER)**: 0.3217 (32.17%)
- **Sneeze Recall**: 2/4 (50.0%)
- **False Positives**: 0

**Missed Sneezes:**
1. Reference: "Take your time, it'll come. SNEEZE Oh wow. Excuse me."
   Prediction: "Take your time. It'll come. Oh, wow."

2. Reference: "It's right there but... False alarm? No, it's stuck. SNEEZE Bless you."
   Prediction: "It's right there, but... False alarm? No! It stopped..."

**Analysis:**
- The model achieved perfect WER (0.0%) on the validation set during training, indicating good generalization on the training distribution.
- On the test set, the model achieved 50% sneeze recall, successfully detecting 2 out of 4 sneezes.
- No false positives were detected, showing the model is conservative in its sneeze predictions.
- The 32.17% WER on the test set suggests room for improvement, particularly in detecting sneezes in more varied contexts.

## Project Structure

```
whisper-adapter-test/
├── prepare_sneeze_data.py      # Data preparation script
├── improved_sneeze_trainer.py   # Training script
├── evaluate_sneeze_model.py     # Evaluation script
├── sneeze_data.jsonl            # Input transcript with sneezes
├── train.jsonl                  # Training manifest
├── test.jsonl                   # Test manifest
├── sneeze_chunks/               # Extracted audio chunks
└── sneeze_lora_adapter_unsloth/ # Trained adapter (created after training)
```

## Output Files

- `train.jsonl`: Training dataset manifest
- `test.jsonl`: Test dataset manifest
- `sneeze_chunks/`: Directory with extracted audio chunks
- `sneeze_lora_adapter_unsloth/`: Trained LoRA adapter weights

## Notes

- The model replaces `<sneeze>` tags with `SNEEZE` during training
- LoRA adapters are memory-efficient and only update a small portion of model weights
- The evaluation script merges the adapter into the base model for inference

## Conclusion

Despite training on only 13 examples and evaluating on 10 test samples, the model achieved significant progress in sneeze detection. With just this small dataset, we were able to fine-tune the Whisper model to recognize and transcribe sneezes with 50% recall and zero false positives. This demonstrates the effectiveness of LoRA adapters for efficient fine-tuning on specialized tasks with limited data.
