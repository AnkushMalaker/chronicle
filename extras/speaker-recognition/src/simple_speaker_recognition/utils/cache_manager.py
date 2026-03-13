"""Caching utilities for expensive operations like Deepgram API calls."""

import hashlib
import json
import os
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Union


class CacheManager:
    """Manages caching for expensive operations like transcription."""

    def __init__(self, cache_dir: str = "cache"):
        """
        Initialize cache manager.

        Args:
            cache_dir: Directory to store cache files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        # Create subdirectories for different cache types
        self.deepgram_cache_dir = self.cache_dir / "deepgram"
        self.deepgram_cache_dir.mkdir(exist_ok=True)

        self.transcript_cache_dir = self.cache_dir / "transcripts"
        self.transcript_cache_dir.mkdir(exist_ok=True)

    @staticmethod
    def get_file_hash(file_content: Union[bytes, str]) -> str:
        """
        Calculate MD5 hash of file content for consistent identification.

        Args:
            file_content: File content as bytes or file path as string

        Returns:
            MD5 hash as hexadecimal string
        """
        if isinstance(file_content, str):
            # Assume it's a file path
            with open(file_content, "rb") as f:
                file_content = f.read()

        return hashlib.md5(file_content).hexdigest()

    @staticmethod
    def get_params_hash(params: Dict[str, Any]) -> str:
        """
        Calculate hash for API parameters.

        Args:
            params: Dictionary of parameters

        Returns:
            MD5 hash of serialized parameters
        """
        # Sort parameters for consistent hashing
        sorted_params = json.dumps(params, sort_keys=True)
        return hashlib.md5(sorted_params.encode()).hexdigest()

    def get_cache_key(
        self, file_content: Union[bytes, str], params: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Generate cache key from file content and parameters.

        Args:
            file_content: File content or path
            params: Optional parameters that affect the result

        Returns:
            Cache key string
        """
        file_hash = self.get_file_hash(file_content)

        if params:
            params_hash = self.get_params_hash(params)
            return f"{file_hash}_{params_hash}"

        return file_hash

    def _get_cache_metadata_path(self, cache_file: Path) -> Path:
        """Get path for cache metadata file."""
        return cache_file.with_suffix(cache_file.suffix + ".meta")

    def _save_cache_metadata(self, cache_file: Path, metadata: Dict[str, Any]) -> None:
        """Save cache metadata."""
        meta_path = self._get_cache_metadata_path(cache_file)
        with open(meta_path, "w") as f:
            json.dump(metadata, f, default=str, indent=2)

    def _load_cache_metadata(self, cache_file: Path) -> Optional[Dict[str, Any]]:
        """Load cache metadata."""
        meta_path = self._get_cache_metadata_path(cache_file)
        if meta_path.exists():
            try:
                with open(meta_path, "r") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def cache_deepgram_response(
        self,
        audio_file: Union[str, bytes],
        response: Any,
        params: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Cache Deepgram API response.

        Args:
            audio_file: Audio file path or content
            response: Deepgram response object
            params: Parameters used for the API call
            metadata: Additional metadata to store

        Returns:
            Cache key for the stored response
        """
        cache_key = self.get_cache_key(audio_file, params)
        cache_file = self.deepgram_cache_dir / f"{cache_key}.json"

        try:
            # Convert response to dict if needed
            if hasattr(response, "to_dict"):
                response_dict = response.to_dict()
            elif hasattr(response, "__dict__"):
                response_dict = response.__dict__
            else:
                response_dict = dict(response)

            # Save response
            with open(cache_file, "w") as f:
                json.dump(response_dict, f, indent=2, default=str)

            # Save metadata
            cache_metadata = {
                "cached_at": datetime.now().isoformat(),
                "cache_key": cache_key,
                "api_params": params or {},
                "metadata": metadata or {},
            }

            if isinstance(audio_file, str) and os.path.exists(audio_file):
                cache_metadata["original_file"] = str(Path(audio_file).name)
                cache_metadata["file_size"] = os.path.getsize(audio_file)

            self._save_cache_metadata(cache_file, cache_metadata)

            return cache_key

        except Exception as e:
            raise RuntimeError(f"Failed to cache Deepgram response: {str(e)}")

    def get_cached_deepgram_response(
        self,
        audio_file: Union[str, bytes],
        params: Optional[Dict[str, Any]] = None,
        max_age_hours: float = 24,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached Deepgram response.

        Args:
            audio_file: Audio file path or content
            params: Parameters used for the API call
            max_age_hours: Maximum age of cache in hours (0 for no limit)

        Returns:
            Cached response dict or None if not found/expired
        """
        cache_key = self.get_cache_key(audio_file, params)
        cache_file = self.deepgram_cache_dir / f"{cache_key}.json"

        if not cache_file.exists():
            return None

        # Check cache age if specified
        if max_age_hours > 0:
            metadata = self._load_cache_metadata(cache_file)
            if metadata:
                cached_at = datetime.fromisoformat(metadata["cached_at"])
                if datetime.now() - cached_at > timedelta(hours=max_age_hours):
                    return None

        try:
            with open(cache_file, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def cache_processed_transcript(
        self,
        audio_file: Union[str, bytes],
        segments: list,
        processing_params: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Cache processed transcript segments.

        Args:
            audio_file: Audio file path or content
            segments: Processed transcript segments
            processing_params: Parameters used for processing
            metadata: Additional metadata

        Returns:
            Cache key for the stored transcript
        """
        cache_key = self.get_cache_key(audio_file, processing_params)
        cache_file = self.transcript_cache_dir / f"{cache_key}.json"

        try:
            transcript_data = {
                "segments": segments,
                "processing_params": processing_params or {},
                "processed_at": datetime.now().isoformat(),
                "total_segments": len(segments),
                "total_duration": sum(
                    seg.get("end", 0) - seg.get("start", 0) for seg in segments
                ),
            }

            with open(cache_file, "w") as f:
                json.dump(transcript_data, f, indent=2, default=str)

            # Save metadata
            cache_metadata = {
                "cached_at": datetime.now().isoformat(),
                "cache_key": cache_key,
                "processing_params": processing_params or {},
                "metadata": metadata or {},
                "segment_count": len(segments),
            }

            if isinstance(audio_file, str) and os.path.exists(audio_file):
                cache_metadata["original_file"] = str(Path(audio_file).name)

            self._save_cache_metadata(cache_file, cache_metadata)

            return cache_key

        except Exception as e:
            raise RuntimeError(f"Failed to cache processed transcript: {str(e)}")

    def get_cached_transcript(
        self,
        audio_file: Union[str, bytes],
        processing_params: Optional[Dict[str, Any]] = None,
        max_age_hours: float = 24,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached processed transcript.

        Args:
            audio_file: Audio file path or content
            processing_params: Parameters used for processing
            max_age_hours: Maximum age of cache in hours (0 for no limit)

        Returns:
            Cached transcript data or None if not found/expired
        """
        cache_key = self.get_cache_key(audio_file, processing_params)
        cache_file = self.transcript_cache_dir / f"{cache_key}.json"

        if not cache_file.exists():
            return None

        # Check cache age if specified
        if max_age_hours > 0:
            metadata = self._load_cache_metadata(cache_file)
            if metadata:
                cached_at = datetime.fromisoformat(metadata["cached_at"])
                if datetime.now() - cached_at > timedelta(hours=max_age_hours):
                    return None

        try:
            with open(cache_file, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def clear_cache(
        self, cache_type: Optional[str] = None, max_age_hours: Optional[float] = None
    ) -> Dict[str, int]:
        """
        Clear cache files.

        Args:
            cache_type: Type of cache to clear ('deepgram', 'transcripts', or None for all)
            max_age_hours: Only clear files older than this (None for all)

        Returns:
            Dictionary with counts of cleared files
        """
        cleared = {"deepgram": 0, "transcripts": 0}

        cache_dirs = []
        if cache_type is None:
            cache_dirs = [
                ("deepgram", self.deepgram_cache_dir),
                ("transcripts", self.transcript_cache_dir),
            ]
        elif cache_type == "deepgram":
            cache_dirs = [("deepgram", self.deepgram_cache_dir)]
        elif cache_type == "transcripts":
            cache_dirs = [("transcripts", self.transcript_cache_dir)]

        for cache_name, cache_dir in cache_dirs:
            for cache_file in cache_dir.glob("*.json"):
                should_clear = True

                if max_age_hours is not None:
                    metadata = self._load_cache_metadata(cache_file)
                    if metadata:
                        cached_at = datetime.fromisoformat(metadata["cached_at"])
                        if datetime.now() - cached_at <= timedelta(hours=max_age_hours):
                            should_clear = False

                if should_clear:
                    try:
                        cache_file.unlink()
                        # Also remove metadata file
                        meta_file = self._get_cache_metadata_path(cache_file)
                        if meta_file.exists():
                            meta_file.unlink()
                        cleared[cache_name] += 1
                    except Exception:
                        pass

        return cleared

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        stats = {
            "deepgram": {"count": 0, "total_size": 0, "oldest": None, "newest": None},
            "transcripts": {
                "count": 0,
                "total_size": 0,
                "oldest": None,
                "newest": None,
            },
        }

        for cache_name, cache_dir in [
            ("deepgram", self.deepgram_cache_dir),
            ("transcripts", self.transcript_cache_dir),
        ]:
            for cache_file in cache_dir.glob("*.json"):
                if cache_file.name.endswith(".meta"):
                    continue

                stats[cache_name]["count"] += 1
                stats[cache_name]["total_size"] += cache_file.stat().st_size

                # Get creation time from metadata if available
                metadata = self._load_cache_metadata(cache_file)
                if metadata and "cached_at" in metadata:
                    cached_at = datetime.fromisoformat(metadata["cached_at"])

                    if (
                        stats[cache_name]["oldest"] is None
                        or cached_at < stats[cache_name]["oldest"]
                    ):
                        stats[cache_name]["oldest"] = cached_at

                    if (
                        stats[cache_name]["newest"] is None
                        or cached_at > stats[cache_name]["newest"]
                    ):
                        stats[cache_name]["newest"] = cached_at

        return stats


# Global cache manager instance
_global_cache_manager = None


def get_cache_manager() -> CacheManager:
    """Get the global cache manager instance."""
    global _global_cache_manager
    if _global_cache_manager is None:
        _global_cache_manager = CacheManager()
    return _global_cache_manager


# Convenience functions
def get_file_hash(file_content: Union[bytes, str]) -> str:
    """Calculate MD5 hash of file content for consistent identification."""
    return CacheManager.get_file_hash(file_content)


def cache_deepgram_response(
    audio_file: Union[str, bytes],
    response: Any,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Cache Deepgram API response using global cache manager."""
    return get_cache_manager().cache_deepgram_response(audio_file, response, params)


def get_cached_deepgram_response(
    audio_file: Union[str, bytes], params: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Get cached Deepgram response using global cache manager."""
    return get_cache_manager().get_cached_deepgram_response(audio_file, params)


def cache_transcript_segments(
    audio_file: Union[str, bytes],
    segments: list,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Cache processed transcript segments using global cache manager."""
    return get_cache_manager().cache_processed_transcript(audio_file, segments, params)


def get_cached_transcript_segments(
    audio_file: Union[str, bytes], params: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Get cached transcript segments using global cache manager."""
    return get_cache_manager().get_cached_transcript(audio_file, params)
