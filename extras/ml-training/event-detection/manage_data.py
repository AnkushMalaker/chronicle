import argparse
import json
import os
import shutil
from datetime import datetime

import librosa
import torch
from transformers import WhisperForConditionalGeneration, WhisperProcessor


def parse_args():
    parser = argparse.ArgumentParser(description="Manage Event Detection Dataset")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Bootstrap command
    cmd_bootstrap = subparsers.add_parser(
        "bootstrap", help="Create initial manifest from folder"
    )
    cmd_bootstrap.add_argument(
        "--audio_dir", required=True, help="Directory containing positive audio samples"
    )
    cmd_bootstrap.add_argument(
        "--output_manifest", default="train.jsonl", help="Output jsonl file"
    )
    cmd_bootstrap.add_argument(
        "--source_tag", default="<event>", help="Tag to use for event"
    )

    # Feedback command
    cmd_feedback = subparsers.add_parser(
        "feedback", help="Add feedback (positive/negative)"
    )
    cmd_feedback.add_argument("--audio_path", required=True, help="Path to audio file")
    cmd_feedback.add_argument(
        "--is_positive",
        action="store_true",
        help="Flag if sample is positive instance of event",
    )
    cmd_feedback.add_argument(
        "--manifest", default="train.jsonl", help="Manifest file to update"
    )
    cmd_feedback.add_argument(
        "--source_tag", default="<event>", help="Tag to use if positive"
    )
    cmd_feedback.add_argument(
        "--base_model",
        default="unsloth/whisper-large-v3",
        help="Base model for transcription",
    )

    return parser.parse_args()


def transcribe_audio(audio_path, model_id):
    """Transcribe audio using base model to get ground truth text"""
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Transcribing with {model_id} on {device}...")

    processor = WhisperProcessor.from_pretrained(model_id)
    model = WhisperForConditionalGeneration.from_pretrained(
        model_id, torch_dtype=torch.float16 if device == "cuda" else torch.float32
    )
    model.to(device)
    model.eval()

    audio, _ = librosa.load(audio_path, sr=16000)
    inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
    input_features = inputs.input_features.to(device)

    if device == "cuda":
        input_features = input_features.half()

    with torch.no_grad():
        generated_ids = model.generate(input_features)

    text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()
    return text


def bootstrap(audio_dir, output_manifest, source_tag):
    if not os.path.exists(audio_dir):
        raise FileNotFoundError(f"{audio_dir} does not exist")

    entries = []
    files = [
        f for f in os.listdir(audio_dir) if f.lower().endswith((".wav", ".mp3", ".m4a"))
    ]

    if not files:
        print("No audio files found in directory.")
        return

    print(f"Found {len(files)} files. Adding to {output_manifest}...")

    # Check if we should append or overwrite? Spec implies bootstrapping starts the process.
    # I'll append if exists, or create new.
    mode = "a" if os.path.exists(output_manifest) else "w"

    with open(output_manifest, mode) as f:
        for filename in files:
            file_path = os.path.join(audio_dir, filename)
            # For bootstrapping, we assume these are just the event itself,
            # so text is just the tag.
            # Realistically, we might want to transcribe it, but let's keep it simple for now
            # as these might be non-speech sounds (sneezes).
            entry = {"audio": file_path, "text": source_tag}
            f.write(json.dumps(entry) + "\n")
            print(f"Added {filename}")


def add_feedback(audio_path, is_positive, manifest_path, source_tag, base_model):
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"{audio_path} not found")

    # Get ground truth text
    # If positive, we want "Transcription <event>" or just "<event>" if it's non-speech?
    # If negative, we want "Transcription" (without tag).

    base_text = transcribe_audio(audio_path, base_model)
    print(f"Base transcription: {base_text}")

    if is_positive:
        # If it's a positive sample, we ensure the tag is present.
        # If the model didn't transcribe it (likely), we append it.
        # If it's a pronunciation correction, the user might want to replace a specific word...
        # But for "arbitrary event detection" usually implies adding a marker.
        # Simple heuristic: Append tag to text.
        final_text = f"{base_text} {source_tag}".strip()
    else:
        # Negative sample: The text is just what the base model hears (without the tag)
        final_text = base_text.replace(
            source_tag, ""
        )  # Ensure tag isn't there by accident

    entry = {
        "audio": audio_path,
        "text": final_text,
        "timestamp": datetime.now().isoformat(),
        "type": "positive" if is_positive else "negative",
    }

    with open(manifest_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    print(
        f"Added {'positive' if is_positive else 'negative'} feedback for {audio_path}"
    )


def main():
    args = parse_args()

    if args.command == "bootstrap":
        bootstrap(args.audio_dir, args.output_manifest, args.source_tag)
    elif args.command == "feedback":
        add_feedback(
            args.audio_path,
            args.is_positive,
            args.manifest,
            args.source_tag,
            args.base_model,
        )


if __name__ == "__main__":
    main()
