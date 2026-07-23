"""
Capture VibeVoice transcription output as ground truth for regression tests.

Sends the 4-minute test audio to a running VibeVoice service and saves the
full response (segments, text, words) as a JSON fixture.

To exercise batching/stitching on the 4-min file, restart the service with
small batch windows first:

    cd extras/asr-services
    BATCH_THRESHOLD_SECONDS=60 BATCH_DURATION_SECONDS=90 BATCH_OVERLAP_SECONDS=15 \
        docker compose up vibevoice-asr -d

Then run the capture:

    uv run python tests/capture_vibevoice_ground_truth.py

To restore normal settings afterwards:

    docker compose up vibevoice-asr -d      # env vars unset → uses config defaults

The output is saved to tests/fixtures/vibevoice_4min_ground_truth.json.
Review the output manually before committing — it becomes the reference
for segment overlap and stitching regression tests.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

DEFAULT_AUDIO = (
    Path(__file__).parent.parent.parent.parent
    / "tests"
    / "test_assets"
    / "DIY_Experts_Glass_Blowing_16khz_mono_4min.wav"
)
DEFAULT_OUTPUT = Path(__file__).parent / "fixtures" / "vibevoice_4min_ground_truth.json"


def capture(service_url: str, audio_path: str, output_path: str) -> dict:
    """Send audio to VibeVoice /transcribe and return the parsed result."""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        print(f"ERROR: Audio file not found: {audio_path}")
        sys.exit(1)

    print(f"Sending {audio_path.name} to {service_url}/transcribe ...")

    with open(audio_path, "rb") as f:
        # VibeVoice may return NDJSON for long audio — read the full
        # response and extract the final "result" event.
        with httpx.Client(timeout=httpx.Timeout(600.0)) as client:
            resp = client.post(
                f"{service_url}/transcribe",
                files={"file": (audio_path.name, f, "audio/wav")},
            )
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")

            if "application/x-ndjson" in content_type:
                # Parse NDJSON — last "result" line is the transcription
                data = None
                for line in resp.text.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("type") == "progress":
                        current = event.get("current", "?")
                        total = event.get("total", "?")
                        print(f"  Progress: batch {current}/{total}")
                    elif event.get("type") == "result":
                        data = event
                if data is None:
                    print("ERROR: NDJSON stream ended without a result event")
                    sys.exit(1)
            else:
                data = resp.json()

    # Summarize
    segments = data.get("segments", [])
    words = data.get("words", [])
    text = data.get("text", "")

    print(f"\nResult summary:")
    print(f"  Text length: {len(text)} chars")
    print(f"  Segments: {len(segments)}")
    print(f"  Words: {len(words)}")

    if segments:
        last_seg = segments[-1]
        print(f"  Duration: {last_seg.get('end', 0):.1f}s")

        # Check for overlaps
        overlaps = []
        for i in range(len(segments) - 1):
            overlap = segments[i]["end"] - segments[i + 1]["start"]
            if overlap > 0.5:
                overlaps.append(
                    (
                        i,
                        i + 1,
                        overlap,
                        segments[i]["text"][:40],
                        segments[i + 1]["text"][:40],
                    )
                )

        if overlaps:
            print(f"\n  WARNING: {len(overlaps)} segment overlaps detected!")
            for idx_a, idx_b, dur, text_a, text_b in overlaps:
                print(
                    f'    [{idx_a}→{idx_b}] {dur:.2f}s overlap: "{text_a}" / "{text_b}"'
                )
        else:
            print(f"  No segment overlaps detected")

    # Save
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nSaved to {output}")
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Capture VibeVoice ground truth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
To force batching on the 4-min test file, restart the service first:

    cd extras/asr-services
    BATCH_THRESHOLD_SECONDS=60 BATCH_DURATION_SECONDS=90 BATCH_OVERLAP_SECONDS=15 \\
        docker compose up vibevoice-asr -d

Then run this script. Restore defaults after with:

    docker compose up vibevoice-asr -d
""",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8767",
        help="VibeVoice service URL (default: http://localhost:8767)",
    )
    parser.add_argument(
        "--audio",
        default=str(DEFAULT_AUDIO),
        help="Path to test audio file",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output JSON path",
    )
    args = parser.parse_args()
    capture(args.url, args.audio, args.output)


if __name__ == "__main__":
    main()
