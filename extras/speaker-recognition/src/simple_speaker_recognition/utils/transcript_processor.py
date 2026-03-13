"""Transcript processing utilities for speaker inference."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class TranscriptProcessor:
    """Utilities for processing and formatting transcript data."""

    @staticmethod
    def extract_segments_from_deepgram(response: Any) -> List[Dict[str, Any]]:
        """
        Extract structured segments from Deepgram response.

        Args:
            response: Deepgram API response object

        Returns:
            List of segment dictionaries with speaker, start, end, text
        """
        segments = []

        try:
            # Convert response to dict if needed
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response)

            # Extract utterances (which include speaker diarization)
            if "results" in response_dict and "utterances" in response_dict["results"]:
                for utterance in response_dict["results"]["utterances"]:
                    segments.append(
                        {
                            "speaker": utterance.get("speaker", 0),
                            "start": float(utterance.get("start", 0)),
                            "end": float(utterance.get("end", 0)),
                            "text": utterance.get("transcript", "").strip(),
                        }
                    )

            # Sort segments by start time
            segments.sort(key=lambda x: x["start"])

        except Exception as e:
            raise ValueError(
                f"Error extracting segments from Deepgram response: {str(e)}"
            )

        return segments

    @staticmethod
    def validate_segments(segments: List[Dict[str, Any]]) -> List[str]:
        """
        Validate transcript segments and return list of issues found.

        Args:
            segments: List of segment dictionaries

        Returns:
            List of validation error messages
        """
        issues = []

        if not segments:
            issues.append("No segments provided")
            return issues

        for i, segment in enumerate(segments):
            # Check required fields
            required_fields = ["speaker", "start", "end", "text"]
            for field in required_fields:
                if field not in segment:
                    issues.append(f"Segment {i+1}: Missing required field '{field}'")

            # Check data types and values
            try:
                start = float(segment.get("start", 0))
                end = float(segment.get("end", 0))
                speaker = int(segment.get("speaker", 0))

                if start < 0:
                    issues.append(f"Segment {i+1}: Start time cannot be negative")
                if end < 0:
                    issues.append(f"Segment {i+1}: End time cannot be negative")
                if end <= start:
                    issues.append(
                        f"Segment {i+1}: End time must be greater than start time"
                    )
                if speaker < 0:
                    issues.append(f"Segment {i+1}: Speaker ID cannot be negative")

            except (ValueError, TypeError):
                issues.append(
                    f"Segment {i+1}: Invalid numeric values for time or speaker"
                )

        return issues

    @staticmethod
    def merge_consecutive_segments(
        segments: List[Dict[str, Any]],
        max_gap: float = 1.0,
        same_speaker_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Merge consecutive segments from the same speaker with small gaps.

        Args:
            segments: List of segment dictionaries
            max_gap: Maximum gap in seconds to merge across
            same_speaker_only: Only merge segments from the same speaker

        Returns:
            List of merged segments
        """
        if not segments:
            return []

        # Sort by start time
        sorted_segments = sorted(segments, key=lambda x: x["start"])
        merged = [sorted_segments[0].copy()]

        for current in sorted_segments[1:]:
            last_merged = merged[-1]

            # Check if we should merge
            gap = current["start"] - last_merged["end"]
            same_speaker = current["speaker"] == last_merged["speaker"]

            if gap <= max_gap and (not same_speaker_only or same_speaker):
                # Merge segments
                last_merged["end"] = current["end"]
                last_merged["text"] += f" {current['text']}"
            else:
                # Add as new segment
                merged.append(current.copy())

        return merged

    @staticmethod
    def filter_short_segments(
        segments: List[Dict[str, Any]], min_duration: float = 0.5
    ) -> List[Dict[str, Any]]:
        """
        Filter out segments shorter than minimum duration.

        Args:
            segments: List of segment dictionaries
            min_duration: Minimum segment duration in seconds

        Returns:
            List of filtered segments
        """
        return [
            segment
            for segment in segments
            if (segment["end"] - segment["start"]) >= min_duration
        ]

    @staticmethod
    def get_speaker_statistics(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Calculate statistics about speakers in the transcript.

        Args:
            segments: List of segment dictionaries

        Returns:
            Dictionary with speaker statistics
        """
        if not segments:
            return {
                "total_speakers": 0,
                "total_segments": 0,
                "total_duration": 0.0,
                "speaker_breakdown": {},
            }

        speaker_stats = {}

        for segment in segments:
            speaker_id = segment["speaker"]
            duration = segment["end"] - segment["start"]

            if speaker_id not in speaker_stats:
                speaker_stats[speaker_id] = {
                    "segments": 0,
                    "total_duration": 0.0,
                    "text_length": 0,
                }

            speaker_stats[speaker_id]["segments"] += 1
            speaker_stats[speaker_id]["total_duration"] += duration
            speaker_stats[speaker_id]["text_length"] += len(segment.get("text", ""))

        total_duration = sum(seg["end"] - seg["start"] for seg in segments)

        return {
            "total_speakers": len(speaker_stats),
            "total_segments": len(segments),
            "total_duration": total_duration,
            "speaker_breakdown": speaker_stats,
        }

    @staticmethod
    def format_transcript_text(
        segments: List[Dict[str, Any]],
        include_timestamps: bool = True,
        include_speaker_ids: bool = True,
        speaker_names: Optional[Dict[int, str]] = None,
    ) -> str:
        """
        Format segments into a readable transcript text.

        Args:
            segments: List of segment dictionaries
            include_timestamps: Whether to include timestamps
            include_speaker_ids: Whether to include speaker identifiers
            speaker_names: Optional mapping of speaker IDs to names

        Returns:
            Formatted transcript text
        """
        lines = ["TRANSCRIPT", "=" * 50, ""]

        for segment in segments:
            speaker_id = segment["speaker"]
            text = segment.get("text", "")

            # Determine speaker label
            if speaker_names and speaker_id in speaker_names:
                speaker_label = speaker_names[speaker_id]
            elif include_speaker_ids:
                speaker_label = f"Speaker {speaker_id}"
            else:
                speaker_label = ""

            # Format timestamp
            if include_timestamps:
                start = segment["start"]
                end = segment["end"]
                start_min, start_sec = divmod(int(start), 60)
                end_min, end_sec = divmod(int(end), 60)
                timestamp = (
                    f"[{start_min:02d}:{start_sec:02d} - {end_min:02d}:{end_sec:02d}]"
                )
            else:
                timestamp = ""

            # Combine parts
            parts = [p for p in [speaker_label, timestamp] if p]
            prefix = " ".join(parts)

            if prefix:
                lines.append(f"{prefix}: {text}")
            else:
                lines.append(text)
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def export_to_json(
        segments: List[Dict[str, Any]],
        output_path: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Export segments to JSON file.

        Args:
            segments: List of segment dictionaries
            output_path: Path to output JSON file
            metadata: Optional metadata to include
        """
        data = {
            "metadata": metadata or {},
            "segments": segments,
            "statistics": TranscriptProcessor.get_speaker_statistics(segments),
        }

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load_from_json(input_path: str) -> Dict[str, Any]:
        """
        Load segments from JSON file.

        Args:
            input_path: Path to input JSON file

        Returns:
            Dictionary with segments and metadata
        """
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Validate the loaded data
        if "segments" not in data:
            raise ValueError("JSON file must contain 'segments' field")

        issues = TranscriptProcessor.validate_segments(data["segments"])
        if issues:
            raise ValueError(f"Invalid segments in JSON: {', '.join(issues)}")

        return data

    @staticmethod
    def convert_to_inference_format(segments: List[Dict[str, Any]]) -> str:
        """
        Convert segments to the format expected by the inference API.

        Args:
            segments: List of segment dictionaries

        Returns:
            JSON string formatted for inference API
        """
        # Ensure segments have the correct structure
        formatted_segments = []
        for segment in segments:
            formatted_segments.append(
                {
                    "speaker": int(segment["speaker"]),
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "text": str(segment.get("text", "")),
                }
            )

        return json.dumps(formatted_segments)

    @staticmethod
    def extract_words_from_deepgram(response: Any) -> List[Dict[str, Any]]:
        """
        Extract word-level timestamps from Deepgram response for diarize-identify-match.

        Args:
            response: Deepgram API response object or dictionary

        Returns:
            List of word dictionaries with word, start, end fields
        """
        words = []

        try:
            # Convert response to dict if needed
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response) if response else {}

            # Navigate to words array: results.channels[0].alternatives[0].words
            if "results" in response_dict and "channels" in response_dict["results"]:
                channels = response_dict["results"]["channels"]
                if channels and len(channels) > 0:
                    alternatives = channels[0].get("alternatives", [])
                    if alternatives and len(alternatives) > 0:
                        deepgram_words = alternatives[0].get("words", [])

                        for word_data in deepgram_words:
                            # Extract required fields and handle different possible structures
                            word = word_data.get(
                                "word", word_data.get("punctuated_word", "")
                            )
                            start = float(word_data.get("start", 0.0))
                            end = float(word_data.get("end", 0.0))

                            if word:  # Only add if word is not empty
                                words.append({"word": word, "start": start, "end": end})

        except Exception as e:
            raise ValueError(f"Error extracting words from Deepgram response: {str(e)}")

        return words

    @staticmethod
    def format_for_diarize_match_api(response: Any) -> str:
        """
        Format Deepgram response for the diarize-identify-match API endpoint.

        Args:
            response: Deepgram API response object or dictionary

        Returns:
            JSON string formatted for diarize-identify-match API
        """
        try:
            # Convert response to dict if needed
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response) if response else {}

            # Extract words
            words = TranscriptProcessor.extract_words_from_deepgram(response)

            # Extract full transcript text
            full_text = ""
            if "results" in response_dict and "channels" in response_dict["results"]:
                channels = response_dict["results"]["channels"]
                if channels and len(channels) > 0:
                    alternatives = channels[0].get("alternatives", [])
                    if alternatives and len(alternatives) > 0:
                        full_text = alternatives[0].get("transcript", "")

            # Format for API
            formatted_data = {"words": words, "text": full_text}

            return json.dumps(formatted_data)

        except Exception as e:
            raise ValueError(
                f"Error formatting Deepgram response for diarize-match API: {str(e)}"
            )


# Convenience functions for common operations
def quick_process_deepgram_response(response: Any) -> List[Dict[str, Any]]:
    """Quick processing of Deepgram response with common cleanup."""
    processor = TranscriptProcessor()

    # Extract segments
    segments = processor.extract_segments_from_deepgram(response)

    # Filter short segments
    segments = processor.filter_short_segments(segments, min_duration=0.5)

    # Merge consecutive segments from same speaker
    segments = processor.merge_consecutive_segments(segments, max_gap=1.0)

    return segments


def create_formatted_transcript(
    segments: List[Dict[str, Any]], speaker_names: Optional[Dict[int, str]] = None
) -> str:
    """Create a nicely formatted transcript with speaker names."""
    processor = TranscriptProcessor()
    return processor.format_transcript_text(
        segments,
        include_timestamps=True,
        include_speaker_ids=True,
        speaker_names=speaker_names,
    )
