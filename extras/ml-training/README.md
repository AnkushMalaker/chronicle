# ML Training Scripts

Standalone CLI tools for exporting training data from Chronicle and fine-tuning models. These are **not** part of the backend runtime -- they're run manually on a workstation with GPU access.

## Contents

### `event-detection/`

Export accepted/rejected annotations from MongoDB and train an event detection classifier.

- `export_from_mongo.py` - Export annotation data to training-ready format
- `manage_data.py` - Dataset utilities (split, stats, cleanup)
- `train.py` - Train a classifier from exported data

See `event-detection/README.md` for full usage.

### `whisper-adapter-finetuning/`

Fine-tune a Whisper LoRA adapter for domain-specific ASR improvements (e.g., detecting non-speech events like sneezes, laughter).

- `prepare_sneeze_data.py` - Prepare training data
- `train_sneeze.py` - LoRA adapter training script
- `evaluate_sneeze_model.py` - Evaluate trained adapter

See `whisper-adapter-finetuning/README.md` for full usage.

## Prerequisites

```bash
pip install -r event-detection/requirements.txt
# For whisper adapter: see whisper-adapter-finetuning/README.md
```

## Relationship to Backend

These tools consume annotations created by the backend's annotation system:
- `surface_error_suggestions()` creates `MODEL_SUGGESTION` annotations
- Users accept/reject via the swipe UI
- These scripts export that feedback for model training
