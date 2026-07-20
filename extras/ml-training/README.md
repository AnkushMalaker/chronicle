# ML Training Tools

Standalone CLI tools for exporting Chronicle annotations and training the event detection classifier. These are **not** part of the backend runtime -- they're run manually on a workstation.

## Contents

### `event-detection/`

Export accepted/rejected annotations from MongoDB and train an event detection classifier.

- `export_from_mongo.py` - Export annotation data to training-ready format
- `manage_data.py` - Dataset utilities (split, stats, cleanup)
- `train.py` - Train a classifier from exported data

See `event-detection/README.md` for full usage.

## Prerequisites

```bash
pip install -r event-detection/requirements.txt
```

## Relationship to Backend

These tools consume annotations created by the backend's annotation system:
- `surface_error_suggestions()` creates `MODEL_SUGGESTION` annotations
- Users accept/reject via the swipe UI
- These scripts export that feedback for model training
