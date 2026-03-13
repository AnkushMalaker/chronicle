import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

import yt_dlp
from deepgram import DeepgramClient, PrerecordedOptions
from pydub import AudioSegment

# Import caching utilities
from simple_speaker_recognition.utils.cache_manager import (
    cache_deepgram_response,
    get_cached_deepgram_response,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("transcription.log"), logging.StreamHandler()],
)


class YouTubeTranscriber:
    def __init__(self, deepgram_api_key: str, options: dict[str, str] | None = None):
        self.deepgram_api_key = deepgram_api_key
        self.options = options or {}
        self.deepgram = DeepgramClient(deepgram_api_key)
        self.logger = logging.getLogger(__name__)

    def sanitize_filename(self, title: str) -> str:
        """Sanitize YouTube title for use as filename"""
        # Remove invalid characters and limit length
        sanitized = re.sub(r'[<>:"/\\|?*]', "", title)
        sanitized = re.sub(r"\s+", "-", sanitized.strip())
        return sanitized[:50]  # Limit length

    def download_audio(self, url: str) -> Tuple[str, str]:
        """Download audio from YouTube URL and return audio path and title"""
        try:
            self.logger.info(f"Starting audio download from: {url}")

            # Configure yt-dlp options for audio extraction with fallback formats
            ydl_opts = {
                "format": "bestaudio/best",  # Fallback to best if bestaudio fails
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "wav",
                        "preferredquality": "192",
                    }
                ],
                "postprocessor_args": [
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-acodec",
                    "pcm_s16le",
                ],
                "outtmpl": "%(title)s.%(ext)s",
                "quiet": True,
                "ignoreerrors": False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info first to get title
                self.logger.info("Extracting video information...")
                info = ydl.extract_info(url, download=False)
                title = self.sanitize_filename(info["title"])
                duration = info.get("duration", 0)

                self.logger.info(f"Video title: {info['title']}")
                self.logger.info(f"Sanitized filename: {title}")
                self.logger.info(
                    f"Duration: {duration} seconds ({duration/60:.1f} minutes)"
                )

                # Update output template with sanitized title
                ydl_opts["outtmpl"] = f"{title}.%(ext)s"

                # Download the audio
                self.logger.info("Downloading and converting audio...")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_download:
                    ydl_download.download([url])

            audio_path = f"{title}.wav"

            # Verify the file was created
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"Audio file was not created: {audio_path}")

            file_size = os.path.getsize(audio_path)
            self.logger.info(
                f"Audio download completed: {audio_path} (size: {file_size:,} bytes)"
            )

            return audio_path, title

        except Exception as e:
            self.logger.error(
                f"Audio download failed for {url}: {str(e)}", exc_info=True
            )
            raise

    def segment_audio(self, audio_path: str, title: str) -> List[str]:
        """Segment audio into 10-minute chunks if needed"""
        try:
            self.logger.info(f"Starting audio segmentation for: {audio_path}")

            audio = AudioSegment.from_wav(audio_path)
            duration_ms = len(audio)
            duration_minutes = duration_ms / (60 * 1000)
            segment_duration_ms = 10 * 60 * 1000  # 10 minutes in milliseconds

            self.logger.info(f"Audio duration: {duration_minutes:.1f} minutes")

            segments = []

            if duration_ms <= segment_duration_ms:
                # Audio is 10 minutes or less, rename to include segment number
                new_path = f"{title}-1.wav"
                os.rename(audio_path, new_path)
                segments.append(new_path)
                self.logger.info(f"Audio is ≤10 minutes, renamed to: {new_path}")
            else:
                # Split into 10-minute segments
                num_segments = (
                    duration_ms + segment_duration_ms - 1
                ) // segment_duration_ms
                self.logger.info(
                    f"Splitting into {num_segments} segments of ~10 minutes each"
                )

                for i in range(num_segments):
                    start_ms = i * segment_duration_ms
                    end_ms = min((i + 1) * segment_duration_ms, duration_ms)

                    segment = audio[start_ms:end_ms]
                    segment_path = f"{title}-{i + 1}.wav"

                    self.logger.info(
                        f"Creating segment {i + 1}/{num_segments}: {segment_path}"
                    )
                    segment.export(
                        segment_path,
                        format="wav",
                        parameters=["-ar", "16000", "-ac", "1"],
                    )
                    segments.append(segment_path)

                # Remove original file
                os.remove(audio_path)
                self.logger.info("Removed original audio file after segmentation")

            return segments

        except Exception as e:
            self.logger.error(
                f"Audio segmentation failed for {audio_path}: {str(e)}", exc_info=True
            )
            raise

    def transcribe_audio(
        self, audio_path: str, use_cache: bool = True, diarize: bool = True
    ) -> dict:
        """Transcribe audio using Deepgram Nova-3 with diarization, with caching support"""
        try:
            # Validate file exists and is readable
            if not os.path.exists(audio_path):
                self.logger.error(f"Audio file not found: {audio_path}")
                return None

            file_size = os.path.getsize(audio_path)
            self.logger.info(
                f"Starting transcription of {audio_path} (size: {file_size:,} bytes)"
            )

            # Create parameters for cache key
            api_params = {
                "model": "nova-3",
                "diarize": diarize,
                "multichannel": False,
                "smart_format": True,
                "punctuate": True,
                "language": self.options.get("language", "multi"),
                "utterances": diarize,  # Only use utterances if diarizing
                "paragraphs": True,
            }

            # Check cache first if enabled
            if use_cache:
                self.logger.info(f"Checking cache for {audio_path}")
                cached_response = get_cached_deepgram_response(audio_path, api_params)
                if cached_response:
                    self.logger.info(f"✅ Found cached transcription for {audio_path}")

                    # Create a mock response object that behaves like Deepgram response
                    class CachedResponse:
                        def __init__(self, data):
                            self.results = data.get("results")
                            self._data = data

                        def to_dict(self):
                            return self._data

                    return CachedResponse(cached_response)

            # Read audio file for API call
            with open(audio_path, "rb") as audio_file:
                buffer_data = audio_file.read()

            # Create proper source object for Deepgram SDK
            source = {"buffer": buffer_data}

            options = PrerecordedOptions(
                model="nova-3",
                diarize=diarize,
                multichannel=False,  # Changed to False since we're using mono audio
                smart_format=True,
                punctuate=True,
                language=self.options.get("language", "multi"),
                utterances=diarize,  # Only use utterances if diarizing
                paragraphs=True,
            )

            self.logger.info(
                f"💸 Making Deepgram API call for {audio_path} (no cache found)"
            )

            # Use the correct API method for latest SDK - sync version
            response = self.deepgram.listen.rest.v("1").transcribe_file(
                source=source, options=options
            )

            if response and hasattr(response, "results"):
                self.logger.info(
                    f"Transcription completed successfully for {audio_path}"
                )

                # Cache the response if caching is enabled
                if use_cache:
                    try:
                        cache_key = cache_deepgram_response(
                            audio_path, response, api_params
                        )
                        self.logger.info(
                            f"💾 Cached transcription response with key: {cache_key}"
                        )
                    except Exception as cache_error:
                        self.logger.warning(
                            f"Failed to cache response: {str(cache_error)}"
                        )

                return response
            else:
                self.logger.error(f"Invalid response received for {audio_path}")
                return None

        except Exception as e:
            self.logger.error(
                f"Transcription error for {audio_path}: {str(e)}", exc_info=True
            )
            return None

    def save_raw_json(self, response, title: str, segment_num: int):
        """Save raw Deepgram response as JSON"""
        try:
            raw_json_dir = Path("_raw_json")
            raw_json_dir.mkdir(exist_ok=True)

            filename = f"{title}-{segment_num}.json"
            filepath = raw_json_dir / filename

            # Convert Deepgram response to dictionary
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response)

            with open(filepath, "w") as f:
                json.dump(response_dict, f, indent=2)

            self.logger.info(f"Saved raw JSON to: {filepath}")

        except Exception as e:
            self.logger.error(
                f"Failed to save raw JSON for {title}-{segment_num}: {str(e)}",
                exc_info=True,
            )

    def format_transcript(self, response, title: str, segment_num: int):
        """Format transcript with speaker info and timestamps"""
        try:
            # Convert response to dict if needed
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response)

            if not response_dict or "results" not in response_dict:
                self.logger.warning(
                    f"No results found in response for {title}-{segment_num}"
                )
                return

            output_dir = Path("output")
            output_dir.mkdir(exist_ok=True)

            filename = f"{title}-{segment_num}-transcript.txt"
            filepath = output_dir / filename

            with open(filepath, "w") as f:
                f.write(f"Transcript: {title} - Segment {segment_num}\n")
                f.write("=" * 50 + "\n\n")

                # Use word-level speaker changes for better formatting (like our working approach)
                if (
                    "channels" in response_dict["results"]
                    and response_dict["results"]["channels"]
                ):
                    channels = response_dict["results"]["channels"]
                    if channels and "alternatives" in channels[0]:
                        words = channels[0]["alternatives"][0].get("words", [])

                        if words:
                            # Group words by speaker changes for cleaner output
                            current_speaker = None
                            current_segment = []

                            for word in words:
                                speaker = word.get("speaker", 0)

                                if current_speaker is None:
                                    current_speaker = speaker
                                    current_segment = [word]
                                elif speaker == current_speaker:
                                    current_segment.append(word)
                                else:
                                    # Speaker change - write current segment
                                    if current_segment:
                                        start_time = current_segment[0].get("start", 0)
                                        end_time = current_segment[-1].get("end", 0)
                                        text = " ".join(
                                            [
                                                w.get(
                                                    "punctuated_word", w.get("word", "")
                                                )
                                                for w in current_segment
                                            ]
                                        )

                                        # Format time as MM:SS
                                        start_min, start_sec = divmod(
                                            int(start_time), 60
                                        )
                                        end_min, end_sec = divmod(int(end_time), 60)

                                        f.write(
                                            f"Speaker {current_speaker} [{start_min:02d}:{start_sec:02d} - {end_min:02d}:{end_sec:02d}]: {text}\n\n"
                                        )

                                    # Start new segment
                                    current_speaker = speaker
                                    current_segment = [word]

                            # Write final segment
                            if current_segment:
                                start_time = current_segment[0].get("start", 0)
                                end_time = current_segment[-1].get("end", 0)
                                text = " ".join(
                                    [
                                        w.get("punctuated_word", w.get("word", ""))
                                        for w in current_segment
                                    ]
                                )

                                start_min, start_sec = divmod(int(start_time), 60)
                                end_min, end_sec = divmod(int(end_time), 60)

                                f.write(
                                    f"Speaker {current_speaker} [{start_min:02d}:{start_sec:02d} - {end_min:02d}:{end_sec:02d}]: {text}\n\n"
                                )

                        # Fallback to basic transcript if no words
                        elif "transcript" in channels[0]["alternatives"][0]:
                            f.write(
                                f"Transcript: {channels[0]['alternatives'][0]['transcript']}\n\n"
                            )

                # Fallback for utterances if they exist (legacy support)
                elif "utterances" in response_dict["results"]:
                    for utterance in response_dict["results"]["utterances"]:
                        speaker = utterance.get("speaker", 0)
                        start_time = utterance.get("start", 0)
                        end_time = utterance.get("end", 0)
                        text = utterance.get("transcript", "")

                        # Format time as MM:SS
                        start_min, start_sec = divmod(int(start_time), 60)
                        end_min, end_sec = divmod(int(end_time), 60)

                        f.write(
                            f"Speaker {speaker} [{start_min:02d}:{start_sec:02d} - {end_min:02d}:{end_sec:02d}]: {text}\n\n"
                        )

            self.logger.info(f"Saved transcript to: {filepath}")

        except Exception as e:
            self.logger.error(
                f"Failed to format transcript for {title}-{segment_num}: {str(e)}",
                exc_info=True,
            )

    async def process_youtube_url(self, url: str):
        """Process a single YouTube URL through the entire pipeline"""
        print(f"Processing: {url}")

        # First, get video info to check if files already exist
        try:
            ydl_opts = {"quiet": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                title = self.sanitize_filename(info["title"])
                duration = info.get("duration", 0)
        except Exception as e:
            self.logger.error(f"Failed to extract video info for {url}: {str(e)}")
            raise

        # Check if audio segments already exist
        existing_segments = []
        duration_minutes = duration / 60
        expected_segments = max(1, int((duration + 599) // 600))  # 10-minute segments

        all_segments_exist = True
        for i in range(1, expected_segments + 1):
            segment_path = f"{title}-{i}.wav"
            if os.path.exists(segment_path):
                existing_segments.append(segment_path)
                self.logger.info(f"Found existing audio segment: {segment_path}")
            else:
                all_segments_exist = False
                break

        # Download audio only if segments don't exist
        if all_segments_exist:
            print(
                f"Skipping download - found {len(existing_segments)} existing audio segments"
            )
            segments = existing_segments
        else:
            print("Downloading audio...")
            audio_path, title = self.download_audio(url)

            # Segment audio
            print("Segmenting audio...")
            segments = self.segment_audio(audio_path, title)

        print(f"Created {len(segments)} segment(s)")

        # Transcribe each segment
        for i, segment_path in enumerate(segments, 1):
            # Check if transcription already exists
            raw_json_path = Path("_raw_json") / f"{title}-{i}.json"
            transcript_path = Path("output") / f"{title}-{i}-transcript.txt"

            if raw_json_path.exists() and transcript_path.exists():
                print(
                    f"Skipping transcription for segment {i}/{len(segments)} - files already exist"
                )
                self.logger.info(f"Found existing transcription files for segment {i}")
                continue

            print(f"Transcribing segment {i}/{len(segments)}: {segment_path}")

            response = self.transcribe_audio(segment_path)

            if response:
                # Save raw JSON
                self.save_raw_json(response, title, i)

                # Format and save transcript
                self.format_transcript(response, title, i)

                print(f"Completed segment {i}")
            else:
                print(f"Failed to transcribe segment {i}")

    async def process_urls(self, urls: List[str]):
        """Process multiple YouTube URLs"""
        for url in urls:
            try:
                await self.process_youtube_url(url)
                print(f"Successfully processed: {url}\n")
            except Exception as e:
                print(f"Error processing {url}: {e}\n")
