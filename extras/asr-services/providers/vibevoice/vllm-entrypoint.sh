#!/bin/bash
set -e

MODEL_ID="${1:-microsoft/VibeVoice-ASR}"

# Download model and generate tokenizer files (first run only)
python3 -c "
from huggingface_hub import snapshot_download
from vllm_plugin.tools.generate_tokenizer_files import generate_vibevoice_tokenizer_files
import os

path = snapshot_download('${MODEL_ID}')
marker = os.path.join(path, '.tokenizer_generated')
if not os.path.exists(marker):
    generate_vibevoice_tokenizer_files(path)
    open(marker, 'w').close()
else:
    print('Tokenizer files already generated, skipping.')
print(f'Model path: {path}')
"

exec vllm serve "$@"
