#!/usr/bin/env python3
"""
YouTube Audio Processing CLI Tool

Download YouTube videos, convert to audio formats, and transcribe with Deepgram.

Usage:
    python scripts/youtube_cli.py "https://youtube.com/watch?v=..."
    python scripts/youtube_cli.py "URL" --deepgram-key "your-key"
    python scripts/youtube_cli.py "URL" --output-dir "/path/to/outputs"

Features:
- Downloads YouTube audio in high quality
- Creates both original WAV and 16kHz mono processed WAV
- Transcribes with Deepgram Nova-3 including speaker diarization
- Saves raw JSON response and formatted transcript
- Supports caching to avoid re-processing
- Handles long videos by segmenting into 10-minute chunks
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from simple_speaker_recognition.utils.transcript_processor import TranscriptProcessor
from simple_speaker_recognition.utils.youtube_transcriber import YouTubeTranscriber


class YouTubeAudioCLI:
    def __init__(
        self,
        deepgram_api_key: str,
        output_dir: str = "outputs",
        language: str = "multi",
        diarize: bool = True,
    ):
        self.deepgram_api_key = deepgram_api_key
        self.output_dir = Path(output_dir)
        self.language = language
        self.diarize = diarize
        self.transcriber = YouTubeTranscriber(deepgram_api_key, {"language": language})
        self.transcript_processor = TranscriptProcessor()

        # Create output directories
        self.audio_dir = self.output_dir / "audio"
        self.transcripts_dir = self.output_dir / "transcripts"
        self.json_dir = self.output_dir / "json"

        for dir_path in [self.audio_dir, self.transcripts_dir, self.json_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)

    async def process_youtube_url(self, url: str, save_original: bool = True):
        """Process a YouTube URL with organized output structure"""
        print(f"🎵 Processing YouTube URL: {url}")

        # Get video info first
        try:
            import yt_dlp

            ydl_opts = {"quiet": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                title = self.transcriber.sanitize_filename(info["title"])
                duration = info.get("duration", 0)

            print(f"📹 Video: {info['title']}")
            print(f"⏱️  Duration: {duration//60}:{duration%60:02d}")

        except Exception as e:
            print(f"❌ Failed to extract video info: {e}")
            return False

        try:
            # Step 1: Download original high-quality audio if requested
            original_path = None
            if save_original:
                print("📥 Downloading original high-quality audio...")
                original_path = await self._download_original_audio(url, title)

            # Step 2: Download and process 16kHz mono audio using existing transcriber
            print("🔄 Downloading and processing 16kHz mono audio...")

            # Use existing transcriber pipeline but capture the segments
            # First get the audio using the transcriber
            audio_path, _ = self.transcriber.download_audio(url)
            segments = self.transcriber.segment_audio(audio_path, title)

            # Move processed segments to audio directory
            processed_paths = []
            for segment in segments:
                processed_path = (
                    self.audio_dir
                    / f"{title}-processed-16khz-mono-{segment.split('-')[-1]}"
                )
                os.rename(segment, processed_path)
                processed_paths.append(processed_path)

            print(f"✅ Created {len(processed_paths)} processed audio segment(s)")

            # Step 3: Transcribe segments and organize outputs
            print("🗣️  Transcribing with Deepgram...")

            for i, segment_path in enumerate(processed_paths, 1):
                print(
                    f"   Processing segment {i}/{len(processed_paths)}: {segment_path.name}"
                )

                # Check if transcription already exists
                json_path = self.json_dir / f"{title}-segment-{i}-deepgram-raw.json"
                transcript_path = (
                    self.transcripts_dir / f"{title}-segment-{i}-transcript.txt"
                )

                if json_path.exists() and transcript_path.exists():
                    print(f"   ⏭️  Skipping segment {i} - already transcribed")
                    continue

                # Transcribe segment
                response = self.transcriber.transcribe_audio(
                    str(segment_path), diarize=self.diarize
                )

                if response:
                    # Save raw JSON to json directory
                    self._save_organized_json(response, title, i)

                    # Save formatted transcript to transcripts directory
                    self._save_organized_transcript(response, title, i)

                    print(f"   ✅ Completed segment {i}")
                else:
                    print(f"   ❌ Failed to transcribe segment {i}")

            # Step 4: Create summary with statistics
            self._create_summary(title, original_path, processed_paths, duration)

            print(f"\n🎉 Successfully processed: {title}")
            print(f"📁 Outputs saved to: {self.output_dir}")

            return True

        except Exception as e:
            print(f"❌ Error processing {url}: {e}")
            return False

    async def _download_original_audio(self, url: str, title: str) -> Path:
        """Download original high-quality audio"""
        import yt_dlp

        original_path = self.audio_dir / f"{title}-original.wav"

        if original_path.exists():
            print(f"   ⏭️  Original audio already exists: {original_path.name}")
            return original_path

        ydl_opts = {
            "format": "bestaudio",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                    "preferredquality": "320",  # High quality
                }
            ],
            "outtmpl": str(original_path.with_suffix("")),
            "quiet": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        print(f"   ✅ Original audio saved: {original_path.name}")
        return original_path

    def _save_organized_json(self, response, title: str, segment_num: int):
        """Save raw JSON to organized json directory"""
        json_path = self.json_dir / f"{title}-segment-{segment_num}-deepgram-raw.json"

        try:
            import json

            # Convert response to dict
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response)

            with open(json_path, "w") as f:
                json.dump(response_dict, f, indent=2)

            print(f"   💾 Raw JSON saved: {json_path.name}")

        except Exception as e:
            print(f"   ⚠️  Failed to save JSON: {e}")

    def _save_organized_transcript(self, response, title: str, segment_num: int):
        """Save formatted transcript using TranscriptProcessor utilities"""
        transcript_path = (
            self.transcripts_dir / f"{title}-segment-{segment_num}-transcript.txt"
        )

        try:
            # Extract segments using the robust utility
            segments = self.transcript_processor.extract_segments_from_deepgram(
                response
            )

            if not segments:
                print(f"   ⚠️  No segments extracted for segment {segment_num}")
                return

            # Create header
            header_lines = [
                f"Transcript: {title} - Segment {segment_num}",
                "=" * 60,
                "",
            ]

            # Format transcript with appropriate settings
            if self.diarize:
                # Use speaker diarization formatting
                transcript_content = self.transcript_processor.format_transcript_text(
                    segments,
                    include_timestamps=True,
                    include_speaker_ids=True,
                    speaker_names=None,  # Use generic Speaker 0, 1, etc.
                )
            else:
                # Simple non-diarized format - just concatenate text
                transcript_lines = header_lines + [
                    " ".join(segment["text"] for segment in segments),
                    "",
                ]
                transcript_content = "\n".join(transcript_lines)

            # Write to file
            with open(transcript_path, "w", encoding="utf-8") as f:
                if self.diarize:
                    # The format_transcript_text already includes header
                    f.write(transcript_content)
                else:
                    # Write our simple format
                    f.write(transcript_content)

            # Get statistics for logging
            stats = self.transcript_processor.get_speaker_statistics(segments)
            print(
                f"   📄 Transcript saved: {transcript_path.name} ({stats['total_segments']} segments, {stats['total_speakers']} speakers)"
            )

        except Exception as e:
            print(f"   ⚠️  Failed to save transcript: {e}")
            # Fallback to simple text extraction
            self._save_simple_transcript(response, transcript_path, title, segment_num)

    def _save_simple_transcript(
        self, response, transcript_path: Path, title: str, segment_num: int
    ):
        """Fallback method for simple transcript extraction"""
        try:
            # Convert response to dict
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            else:
                response_dict = dict(response)

            # Extract basic transcript text
            transcript_text = ""
            if (
                "results" in response_dict
                and "channels" in response_dict["results"]
                and response_dict["results"]["channels"]
            ):

                channel = response_dict["results"]["channels"][0]
                if "alternatives" in channel and channel["alternatives"]:
                    transcript_text = channel["alternatives"][0].get(
                        "transcript", "No transcript available"
                    )

            with open(transcript_path, "w", encoding="utf-8") as f:
                f.write(f"Transcript: {title} - Segment {segment_num}\n")
                f.write("=" * 60 + "\n\n")
                f.write(transcript_text + "\n")

            print(f"   📄 Simple transcript saved: {transcript_path.name}")

        except Exception as e:
            print(f"   ❌ Failed to save even simple transcript: {e}")

    def _create_summary(
        self, title: str, original_path: Path, processed_paths: list, duration: int
    ):
        """Create a summary file with processing details"""
        summary_path = self.output_dir / f"{title}-SUMMARY.txt"

        with open(summary_path, "w") as f:
            f.write(f"YouTube Audio Processing Summary\n")
            f.write(f"=" * 40 + "\n\n")
            f.write(f"Title: {title}\n")
            f.write(f"Duration: {duration//60}:{duration%60:02d}\n")
            f.write(f"Segments: {len(processed_paths)}\n")
            f.write(
                f"Processing Date: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )

            f.write("Generated Files:\n")
            f.write("-" * 15 + "\n")

            if original_path and original_path.exists():
                f.write(f"📼 Original Audio: {original_path.name}\n")

            for i, path in enumerate(processed_paths, 1):
                f.write(f"🎵 Processed Audio {i}: {path.name}\n")
                f.write(f"📄 Transcript {i}: {title}-segment-{i}-transcript.txt\n")
                f.write(f"💾 Raw JSON {i}: {title}-segment-{i}-deepgram-raw.json\n")

        print(f"📋 Summary saved: {summary_path.name}")


def main():
    parser = argparse.ArgumentParser(
        description="YouTube Audio Processing CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/youtube_cli.py "https://youtube.com/watch?v=dQw4w9WgXcQ"
  python scripts/youtube_cli.py "URL" --deepgram-key "your-key-here"
  python scripts/youtube_cli.py "URL" --output-dir "./my-outputs" --no-original
        """,
    )

    parser.add_argument("url", help="YouTube URL to process")
    parser.add_argument(
        "--deepgram-key", help="Deepgram API key (or set DEEPGRAM_API_KEY env var)"
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Output directory for all files (default: outputs)",
    )
    parser.add_argument(
        "--no-original",
        action="store_true",
        help="Skip downloading original high-quality audio",
    )
    parser.add_argument(
        "--language",
        default="multi",
        help="Language for transcription (default: multi)",
    )
    parser.add_argument(
        "--no-diarization", action="store_true", help="Disable speaker diarization"
    )

    args = parser.parse_args()

    # Get API key from args or environment
    api_key = args.deepgram_key or os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        print("❌ Error: Deepgram API key required")
        print("   Set --deepgram-key or DEEPGRAM_API_KEY environment variable")
        return 1

    # Validate URL
    if not ("youtube.com" in args.url or "youtu.be" in args.url):
        print("❌ Error: Please provide a valid YouTube URL")
        return 1

    print("🚀 YouTube Audio Processing CLI")
    print("=" * 40)

    # Create CLI instance and process
    cli = YouTubeAudioCLI(
        api_key,
        args.output_dir,
        language=args.language,
        diarize=not args.no_diarization,
    )

    try:
        # Run async processing
        success = asyncio.run(
            cli.process_youtube_url(args.url, save_original=not args.no_original)
        )

        return 0 if success else 1

    except KeyboardInterrupt:
        print("\n⚠️  Processing interrupted by user")
        return 1
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
