#!/usr/bin/env python3
"""
Convert HuggingFace Whisper model to CTranslate2 format.

This enables using any HuggingFace Whisper model with faster-whisper.

Usage:
    python scripts/convert_to_ct2.py openai/whisper-large-v3 ./models/whisper-large-v3-ct2
    python scripts/convert_to_ct2.py Oriserve/Whisper-Hindi2Hinglish-Prime ./models/whisper-hindi-ct2 --quantization float16

Requirements:
    pip install transformers ctranslate2
"""

import argparse
import subprocess
import sys
from pathlib import Path


def convert_model(
    hf_model: str,
    output_dir: str,
    quantization: str = "float16",
    force: bool = False,
) -> None:
    """
    Convert a HuggingFace Whisper model to CTranslate2 format.

    Args:
        hf_model: HuggingFace model identifier (e.g., "openai/whisper-large-v3")
        output_dir: Output directory for converted model
        quantization: Quantization type (float16, int8, float32)
        force: Overwrite existing output directory
    """
    output_path = Path(output_dir)

    if output_path.exists() and not force:
        print(f"Output directory already exists: {output_path}")
        print("Use --force to overwrite")
        sys.exit(1)

    print(f"Converting {hf_model} to CTranslate2 format...")
    print(f"  Output: {output_path}")
    print(f"  Quantization: {quantization}")
    print()

    # Build conversion command
    cmd = [
        "ct2-transformers-converter",
        "--model",
        hf_model,
        "--output_dir",
        str(output_path),
        "--quantization",
        quantization,
        "--copy_files",
        "tokenizer.json",
        "preprocessor_config.json",
    ]

    if force:
        cmd.append("--force")

    print(f"Running: {' '.join(cmd)}")
    print()

    try:
        result = subprocess.run(cmd, check=True)
        print()
        print(f"Conversion successful!")
        print(f"Model saved to: {output_path}")
        print()
        print("To use with faster-whisper:")
        print(f"  ASR_MODEL={output_path} docker compose up -d faster-whisper-asr")
    except subprocess.CalledProcessError as e:
        print(f"Conversion failed with error code {e.returncode}")
        sys.exit(e.returncode)
    except FileNotFoundError:
        print("Error: ct2-transformers-converter not found")
        print("Install with: pip install ctranslate2")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Convert HuggingFace Whisper model to CTranslate2 format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Convert standard Whisper model
    python scripts/convert_to_ct2.py openai/whisper-large-v3 ./models/whisper-large-v3-ct2

    # Convert with int8 quantization for smaller size
    python scripts/convert_to_ct2.py openai/whisper-large-v3 ./models/whisper-large-v3-int8 --quantization int8

    # Convert Hindi Whisper model
    python scripts/convert_to_ct2.py Oriserve/Whisper-Hindi2Hinglish-Prime ./models/whisper-hindi-ct2
        """,
    )

    parser.add_argument(
        "model",
        help="HuggingFace model identifier (e.g., openai/whisper-large-v3)",
    )
    parser.add_argument(
        "output_dir",
        help="Output directory for converted model",
    )
    parser.add_argument(
        "--quantization",
        choices=["float16", "int8", "float32"],
        default="float16",
        help="Quantization type (default: float16)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output directory",
    )

    args = parser.parse_args()
    convert_model(args.model, args.output_dir, args.quantization, args.force)


if __name__ == "__main__":
    main()
