"""Parser for Deepgram JSON transcription output."""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DeepgramParser:
    """Parse Deepgram JSON transcription output and extract speaker segments."""

    def __init__(self, min_segment_duration: float = 0.5):
        """Initialize parser.

        Args:
            min_segment_duration: Minimum duration for a segment in seconds
        """
        self.min_segment_duration = min_segment_duration

    def parse_deepgram_json(self, json_path: str) -> Dict[str, Any]:
        """Parse Deepgram JSON file and extract transcript data.

        Args:
            json_path: Path to Deepgram JSON file

        Returns:
            Parsed data with metadata and segments
        """
        with open(json_path, "r") as f:
            data = json.load(f)

        # Extract metadata
        metadata = data.get("metadata", {})
        results = data.get("results", {})

        # Parse channels (usually just one for mono audio)
        channels = results.get("channels", [])
        if not channels:
            raise ValueError("No channels found in Deepgram output")

        # Use first channel (typically mono audio)
        channel = channels[0]
        alternatives = channel.get("alternatives", [])

        if not alternatives:
            raise ValueError("No alternatives found in channel")

        # Use best alternative (highest confidence)
        best_alternative = alternatives[0]

        # Extract words with speaker information
        words = best_alternative.get("words", [])

        # Group words into speaker segments
        segments = self._group_words_by_speaker(words)

        # Extract unique speakers
        unique_speakers = self._extract_unique_speakers(segments)

        return {
            "metadata": metadata,
            "transcript": best_alternative.get("transcript", ""),
            "confidence": best_alternative.get("confidence", 0.0),
            "segments": segments,
            "unique_speakers": unique_speakers,
            "total_duration": metadata.get("duration", 0.0),
        }

    def _group_words_by_speaker(
        self, words: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Group consecutive words by speaker into segments.

        Args:
            words: List of word dictionaries from Deepgram

        Returns:
            List of speaker segments
        """
        if not words:
            return []

        segments = []
        current_segment = None

        for word in words:
            speaker = word.get("speaker", 0)
            speaker_label = f"speaker_{speaker}"

            # Check if we need to start a new segment
            if (
                current_segment is None
                or current_segment["deepgram_speaker_label"] != speaker_label
            ):

                # Save previous segment if it exists and meets minimum duration
                if current_segment is not None:
                    duration = (
                        current_segment["end_time"] - current_segment["start_time"]
                    )
                    if duration >= self.min_segment_duration:
                        segments.append(current_segment)

                # Start new segment
                current_segment = {
                    "deepgram_speaker_label": speaker_label,
                    "speaker": speaker,
                    "start_time": word.get("start", 0.0),
                    "end_time": word.get("end", 0.0),
                    "words": [word],
                    "text": word.get("punctuated_word", word.get("word", "")),
                    "confidence": word.get("confidence", 0.0),
                    "speaker_confidence": word.get("speaker_confidence", 0.0),
                }
            else:
                # Continue current segment
                current_segment["end_time"] = word.get(
                    "end", current_segment["end_time"]
                )
                current_segment["words"].append(word)
                current_segment["text"] += " " + word.get(
                    "punctuated_word", word.get("word", "")
                )

                # Update average confidence
                total_confidence = current_segment["confidence"] * (
                    len(current_segment["words"]) - 1
                )
                total_confidence += word.get("confidence", 0.0)
                current_segment["confidence"] = total_confidence / len(
                    current_segment["words"]
                )

                # Update average speaker confidence
                if "speaker_confidence" in word:
                    total_speaker_conf = current_segment["speaker_confidence"] * (
                        len(current_segment["words"]) - 1
                    )
                    total_speaker_conf += word["speaker_confidence"]
                    current_segment["speaker_confidence"] = total_speaker_conf / len(
                        current_segment["words"]
                    )

        # Don't forget the last segment
        if current_segment is not None:
            duration = current_segment["end_time"] - current_segment["start_time"]
            if duration >= self.min_segment_duration:
                segments.append(current_segment)

        # Clean up text in segments
        for segment in segments:
            segment["text"] = segment["text"].strip()
            # Remove words list to reduce memory usage
            segment.pop("words", None)

        logger.info(
            f"Extracted {len(segments)} speaker segments from {len(words)} words"
        )

        return segments

    def _extract_unique_speakers(self, segments: List[Dict[str, Any]]) -> List[str]:
        """Extract unique speaker labels from segments.

        Args:
            segments: List of speaker segments

        Returns:
            List of unique speaker labels
        """
        speakers = set()
        for segment in segments:
            speakers.add(segment["deepgram_speaker_label"])

        return sorted(list(speakers))

    def convert_to_annotation_format(
        self, parsed_data: Dict[str, Any], audio_file_path: str, user_id: int
    ) -> List[Dict[str, Any]]:
        """Convert parsed Deepgram data to annotation format.

        Args:
            parsed_data: Parsed Deepgram data
            audio_file_path: Path to the audio file
            user_id: User ID for annotations

        Returns:
            List of annotation dictionaries
        """
        annotations = []

        for segment in parsed_data["segments"]:
            annotation = {
                "audio_file_path": audio_file_path,
                "start_time": segment["start_time"],
                "end_time": segment["end_time"],
                "speaker_id": None,  # Will be mapped later
                "speaker_label": segment["deepgram_speaker_label"],
                "deepgram_speaker_label": segment["deepgram_speaker_label"],
                "label": "UNCERTAIN",  # Default quality label
                "confidence": segment.get("confidence", 0.0),
                "speaker_confidence": segment.get("speaker_confidence", 0.0),
                "transcription": segment["text"],
                "user_id": user_id,
            }
            annotations.append(annotation)

        return annotations

    def get_speaker_statistics(
        self, parsed_data: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """Get statistics for each speaker in the transcript.

        Args:
            parsed_data: Parsed Deepgram data

        Returns:
            Dictionary of speaker statistics
        """
        stats = defaultdict(
            lambda: {
                "segment_count": 0,
                "total_duration": 0.0,
                "total_words": 0,
                "average_confidence": 0.0,
                "average_speaker_confidence": 0.0,
            }
        )

        for segment in parsed_data["segments"]:
            speaker = segment["deepgram_speaker_label"]
            duration = segment["end_time"] - segment["start_time"]

            stats[speaker]["segment_count"] += 1
            stats[speaker]["total_duration"] += duration
            stats[speaker]["total_words"] += len(segment["text"].split())

            # Update running averages
            n = stats[speaker]["segment_count"]
            stats[speaker]["average_confidence"] = (
                stats[speaker]["average_confidence"] * (n - 1) + segment["confidence"]
            ) / n
            stats[speaker]["average_speaker_confidence"] = (
                stats[speaker]["average_speaker_confidence"] * (n - 1)
                + segment.get("speaker_confidence", 0.0)
            ) / n

        # Calculate speaking rate
        for speaker, data in stats.items():
            if data["total_duration"] > 0:
                data["words_per_minute"] = (
                    data["total_words"] / data["total_duration"]
                ) * 60
            else:
                data["words_per_minute"] = 0

        return dict(stats)

    def merge_short_segments(
        self, segments: List[Dict[str, Any]], min_duration: float = 1.0
    ) -> List[Dict[str, Any]]:
        """Merge very short segments with adjacent segments of the same speaker.

        Args:
            segments: List of segments
            min_duration: Minimum duration to keep segment separate

        Returns:
            List of merged segments
        """
        if not segments:
            return []

        merged = []
        current = segments[0].copy()

        for next_segment in segments[1:]:
            # Check if we should merge
            if (
                current["deepgram_speaker_label"]
                == next_segment["deepgram_speaker_label"]
                and next_segment["start_time"] - current["end_time"] < 0.5
            ):  # Less than 0.5s gap

                # Merge segments
                current["end_time"] = next_segment["end_time"]
                current["text"] += " " + next_segment["text"]

                # Update average confidence
                total_duration = current["end_time"] - current["start_time"]
                current_duration = (
                    current["end_time"]
                    - current["start_time"]
                    - (next_segment["end_time"] - next_segment["start_time"])
                )
                next_duration = next_segment["end_time"] - next_segment["start_time"]

                current["confidence"] = (
                    current["confidence"] * current_duration
                    + next_segment["confidence"] * next_duration
                ) / total_duration
            else:
                # Save current segment and start new one
                merged.append(current)
                current = next_segment.copy()

        # Don't forget the last segment
        merged.append(current)

        logger.info(f"Merged {len(segments)} segments into {len(merged)} segments")

        return merged
