import json
import os
import random

import librosa
import numpy as np
import soundfile as sf


def prepare_data():
    jsonl_path = "sneeze_data.jsonl"
    video_path = "girls_sneezing.mp4"
    output_dir = "sneeze_chunks"

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Load full audio
    print(f"Loading {video_path}...")
    try:
        y, sr = librosa.load(video_path, sr=16000)
    except Exception as e:
        print(f"Error loading video: {e}")
        return

    segments = []
    with open(jsonl_path, "r") as f:
        for line in f:
            if line.strip():
                segments.append(json.loads(line))

    print(f"Found {len(segments)} segments.")

    dataset_entries = []

    for i, seg in enumerate(segments):
        start_time = seg["start"]
        end_time = seg["end"]
        text = seg["text"]

        # Calculate sample indices
        start_sample = int(start_time * sr)
        end_sample = int(end_time * sr)

        # Extract audio
        chunk = y[start_sample:end_sample]

        # Save to file
        chunk_filename = f"chunk_{i:03d}.wav"
        chunk_path = os.path.join(output_dir, chunk_filename)
        sf.write(chunk_path, chunk, sr)

        dataset_entries.append({"audio": chunk_path, "text": text})
        print(f"Saved {chunk_filename}: {text[:30]}...")

    # Shuffle and Split
    random.seed(42)
    random.shuffle(dataset_entries)

    split_idx = int(len(dataset_entries) * 0.6)
    train_data = dataset_entries[:split_idx]
    test_data = dataset_entries[split_idx:]

    print(f"Training samples: {len(train_data)}")
    print(f"Testing samples: {len(test_data)}")

    # Save split manifests
    with open("train.jsonl", "w") as f:
        for entry in train_data:
            f.write(json.dumps(entry) + "\n")

    with open("test.jsonl", "w") as f:
        for entry in test_data:
            f.write(json.dumps(entry) + "\n")

    print("Data preparation complete.")


if __name__ == "__main__":
    prepare_data()
