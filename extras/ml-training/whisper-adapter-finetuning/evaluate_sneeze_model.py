import json
import os

import jiwer
import librosa
import torch
from peft import PeftModel
from tqdm import tqdm
from transformers import WhisperForConditionalGeneration, WhisperProcessor

# --- CONFIGURATION (MUST MATCH YOUR TRAINING) ---
BASE_MODEL_ID = "openai/whisper-large-v3"
ADAPTER_PATH = "sneeze_lora_adapter_unsloth"  # The folder Unsloth created
TEST_MANIFEST = "test.jsonl"


def main():
    # 1. Setup Device
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    # 2. Load Base Model (Large v3)
    print(f"Loading base model: {BASE_MODEL_ID}")
    processor = WhisperProcessor.from_pretrained(BASE_MODEL_ID)
    model = WhisperForConditionalGeneration.from_pretrained(
        BASE_MODEL_ID, torch_dtype=torch.float16 if device == "cuda" else torch.float32
    )

    # 3. Load and MERGE Adapter
    if os.path.exists(ADAPTER_PATH):
        print(f"Loading LoRA adapter from: {ADAPTER_PATH}")
        model = PeftModel.from_pretrained(model, ADAPTER_PATH)
        print("Merging LoRA weights...")
        model = model.merge_and_unload()
    else:
        print(f"❌ ERROR: Adapter {ADAPTER_PATH} not found!")
        return

    model.to(device)
    model.eval()

    # 4. Run Evaluation
    evaluate_dataset(model, processor, device, TEST_MANIFEST)


def evaluate_dataset(model, processor, device, manifest_path):
    if not os.path.exists(manifest_path):
        print(f"Manifest {manifest_path} not found.")
        return

    samples = []
    with open(manifest_path, "r") as f:
        for line in f:
            samples.append(json.loads(line))

    print(f"Testing on {len(samples)} samples...")

    predictions = []
    references = []
    sneeze_stats = {"total": 0, "detected": 0, "fp": 0}

    for sample in tqdm(samples):
        path = sample["audio"]
        ref_text = sample["text"].replace("<sneeze>", "SNEEZE")

        try:
            audio, _ = librosa.load(path, sr=16000)
        except:
            continue

        # Process audio
        inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
        input_features = inputs.input_features.to(device)

        # Handle the dtype for half precision (if on GPU)
        if device == "cuda":
            input_features = input_features.half()

        # Generate
        with torch.no_grad():
            generated_ids = model.generate(
                input_features=input_features,  # Use input_features, not inputs
                language="en",
                task="transcribe",
                max_new_tokens=256,
            )

        pred = processor.batch_decode(generated_ids, skip_special_tokens=True)[
            0
        ].strip()

        predictions.append(pred)
        references.append(ref_text)

        # Stats
        has_sneeze_ref = "SNEEZE" in ref_text
        has_sneeze_pred = "SNEEZE" in pred

        if has_sneeze_ref:
            sneeze_stats["total"] += 1
            if has_sneeze_pred:
                sneeze_stats["detected"] += 1
            else:
                print(f"\n❌ MISSED SNEEZE\nRef: {ref_text}\nPrd: {pred}")
        elif has_sneeze_pred:
            sneeze_stats["fp"] += 1
            print(f"\n⚠️ FALSE POSITIVE\nRef: {ref_text}\nPrd: {pred}")

    # Results
    wer = jiwer.wer(references, predictions)
    print("\n" + "=" * 40)
    print(f"Word Error Rate: {wer:.4f}")
    if sneeze_stats["total"] > 0:
        recall = (sneeze_stats["detected"] / sneeze_stats["total"]) * 100
        print(
            f"Sneeze Recall: {sneeze_stats['detected']}/{sneeze_stats['total']} ({recall:.1f}%)"
        )
    print(f"False Positives: {sneeze_stats['fp']}")
    print("=" * 40)


if __name__ == "__main__":
    main()
