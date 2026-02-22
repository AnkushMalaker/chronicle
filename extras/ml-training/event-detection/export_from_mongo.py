#!/usr/bin/env python3
"""
Export MongoDB Training Stash to JSONL for LoRA Training

Bridge script: User-Loop (MongoDB) → Training (JSONL)

Usage:
    python export_from_mongo.py --output user_loop_feedback.jsonl

Workflow:
    1. Users swipe on user-loop popup
    2. Items saved to MongoDB training_stash collection
    3. Export with this script to JSONL format
    4. Train LoRA adapter: python train.py --train_manifest user_loop_feedback.jsonl
"""

import argparse
import json
from datetime import datetime

from pymongo import MongoClient


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export training stash from MongoDB to JSONL"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="user_loop_feedback.jsonl",
        help="Output JSONL file",
    )
    parser.add_argument(
        "--mongo_uri",
        type=str,
        default="mongodb://localhost:27017",
        help="MongoDB connection string",
    )
    parser.add_argument(
        "--db_name", type=str, default="chronicle", help="Database name"
    )
    parser.add_argument(
        "--min_samples", type=int, default=0, help="Minimum samples to export"
    )
    return parser.parse_args()


def export_training_stash(mongo_uri, db_name, output_file, min_samples):
    """
    Export MongoDB training_stash collection to JSONL format

    Schema:
        {
            "audio": "/path/to/audio.wav",
            "text": "Transcription with <event> tag",
            "timestamp": "2024-01-30T10:00:00Z",
            "type": "positive"
        }
    """
    print(f"🔗 Connecting to MongoDB: {mongo_uri}")
    print(f"📁 Database: {db_name}")

    client = MongoClient(mongo_uri)
    db = client[db_name]

    # Fetch all training stash entries
    entries = list(db.training_stash.find({}))

    if not entries:
        print("❌ No entries found in training_stash collection")
        print("💡 Tip: Swipe right on user-loop popup to add samples")
        return False

    if len(entries) < min_samples:
        print(f"⚠️  Found {len(entries)} entries (minimum: {min_samples})")
        return False

    print(f"✅ Found {len(entries)} entries in training_stash")

    # Convert to JSONL format
    exported = 0
    with open(output_file, "w") as f:
        for entry in entries:
            # Map MongoDB schema to training schema
            training_sample = {
                "audio": f"/data/audio/{entry['conversation_id']}.wav",
                "text": entry["transcript"],
                "timestamp": entry.get("timestamp", datetime.now().isoformat()),
                "type": "positive",  # User-loop rejections = positive for training
            }

            # Write as JSONL (one JSON per line)
            f.write(json.dumps(training_sample) + "\n")
            exported += 1

    print(f"💾 Exported {exported} entries to {output_file}")

    # Print statistics
    print(f"\n📊 Statistics:")
    print(f"   Total exported: {exported}")

    # Count unique conversations
    unique_convs = set(e["conversation_id"] for e in entries)
    print(f"   Unique conversations: {len(unique_convs)}")

    # Check audio data
    has_audio = sum(
        1 for e in entries if e.get("audio_chunks") and len(e["audio_chunks"]) > 0
    )
    print(f"   Has audio chunks: {has_audio}/{exported}")

    return True


def main():
    args = parse_args()

    print("🚀 MongoDB Training Stash Export")
    print("=" * 50)

    success = export_training_stash(
        args.mongo_uri, args.db_name, args.output, args.min_samples
    )

    print("\n" + "=" * 50)

    if success:
        print("✅ Export complete!")
        print("\n🎯 Next Steps:")
        print("   1. Review exported file:", args.output)
        print("   2. Train LoRA adapter:")
        print(f"      python train.py --train_manifest {args.output}")
        print("   3. Run anomaly scan (MongoDB flagging):")
        print(
            "      cd .. && uv run python src/advanced_omi_backend/scripts/run_anomaly_detection.py"
        )
    else:
        print("❌ Export failed")
        print("\n💡 Suggestions:")
        print("   - Swipe left on user-loop popup to add samples")
        print("   - Check MongoDB connection")
        print("   - Lower --min_samples threshold")


if __name__ == "__main__":
    main()
