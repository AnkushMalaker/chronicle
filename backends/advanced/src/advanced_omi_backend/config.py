"""
Configuration management for Chronicle backend.

Currently contains diarization settings because they were used in multiple places 
causing circular imports. Other configurations can be moved here as needed.
"""

import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Data directory paths
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CHUNK_DIR = Path("./audio_chunks")  # Mounted to ./data/audio_chunks by Docker

# Default diarization settings
DEFAULT_DIARIZATION_SETTINGS = {
    "diarization_source": "pyannote",
    "similarity_threshold": 0.15,
    "min_duration": 0.5,
    "collar": 2.0,
    "min_duration_off": 1.5,
    "min_speakers": 2,
    "max_speakers": 6
}

# Default speech detection settings
DEFAULT_SPEECH_DETECTION_SETTINGS = {
    "min_words": 10,              # Minimum words to create conversation (increased from 5)
    "min_confidence": 0.7,        # Word confidence threshold (increased from 0.5)
    "min_duration": 10.0,         # Minimum speech duration in seconds (increased from 2.0)
}

# Default conversation stop settings
DEFAULT_CONVERSATION_STOP_SETTINGS = {
    "transcription_buffer_seconds": 120,    # Periodic transcription interval (2 minutes)
    "speech_inactivity_threshold": 60,      # Speech gap threshold for closure (1 minute)
}

# Default audio storage settings
DEFAULT_AUDIO_STORAGE_SETTINGS = {
    "audio_base_path": "/app/data",  # Main audio directory (where volume is mounted)
    "audio_chunks_path": "/app/audio_chunks",  # Full path to audio chunks subfolder
}

# Global cache for diarization settings
_diarization_settings = None


def get_diarization_config_path():
    """Get the path to the diarization config file."""
    # Try different locations in order of preference
    # 1. Data directory (for persistence across container restarts)
    data_path = Path("/app/data/diarization_config.json")
    if data_path.parent.exists():
        return data_path
    
    # 2. App root directory
    app_path = Path("/app/diarization_config.json")
    if app_path.parent.exists():
        return app_path
    
    # 3. Local development path
    local_path = Path("diarization_config.json")
    return local_path


def load_diarization_settings_from_file():
    """Load diarization settings from file or create from template."""
    global _diarization_settings
    
    config_path = get_diarization_config_path()
    template_path = Path("/app/diarization_config.json.template")
    
    # If no template, try local development path
    if not template_path.exists():
        template_path = Path("diarization_config.json.template")
    
    # If config doesn't exist, try to copy from template
    if not config_path.exists():
        if template_path.exists():
            try:
                # Ensure parent directory exists
                config_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(template_path, config_path)
                logger.info(f"Created diarization config from template at {config_path}")
            except Exception:
                logger.exception(f"Could not copy template to {config_path}")
    
    # Load from file if it exists
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                _diarization_settings = json.load(f)
                logger.info(f"Loaded diarization settings from {config_path}")
                return _diarization_settings
        except Exception as e:
            logger.exception(f"Error loading diarization settings from {config_path}")
    
    # Fall back to defaults
    _diarization_settings = DEFAULT_DIARIZATION_SETTINGS.copy()
    logger.info("Using default diarization settings")
    return _diarization_settings


def save_diarization_settings_to_file(settings):
    """Save diarization settings to file."""
    global _diarization_settings
    
    config_path = get_diarization_config_path()
    
    try:
        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write settings to file
        with open(config_path, 'w') as f:
            json.dump(settings, f, indent=2)
        
        # Update cache
        _diarization_settings = settings
        
        logger.info(f"Saved diarization settings to {config_path}")
        return True
    except Exception as e:
        logger.exception(f"Error saving diarization settings to {config_path}")
        return False


def get_speech_detection_settings():
    """Get speech detection settings from environment or defaults."""

    return {
        "min_words": int(os.getenv("SPEECH_DETECTION_MIN_WORDS", DEFAULT_SPEECH_DETECTION_SETTINGS["min_words"])),
        "min_confidence": float(os.getenv("SPEECH_DETECTION_MIN_CONFIDENCE", DEFAULT_SPEECH_DETECTION_SETTINGS["min_confidence"])),
        "min_duration": float(os.getenv("SPEECH_DETECTION_MIN_DURATION", DEFAULT_SPEECH_DETECTION_SETTINGS["min_duration"])),
    }


def get_conversation_stop_settings():
    """Get conversation stop settings from environment or defaults."""

    return {
        "transcription_buffer_seconds": float(os.getenv("TRANSCRIPTION_BUFFER_SECONDS", DEFAULT_CONVERSATION_STOP_SETTINGS["transcription_buffer_seconds"])),
        "speech_inactivity_threshold": float(os.getenv("SPEECH_INACTIVITY_THRESHOLD_SECONDS", DEFAULT_CONVERSATION_STOP_SETTINGS["speech_inactivity_threshold"])),
        "min_word_confidence": float(os.getenv("SPEECH_DETECTION_MIN_CONFIDENCE", DEFAULT_SPEECH_DETECTION_SETTINGS["min_confidence"])),
    }


def get_audio_storage_settings():
    """Get audio storage settings from environment or defaults."""
    
    # Get base path and derive chunks path
    audio_base_path = os.getenv("AUDIO_BASE_PATH", DEFAULT_AUDIO_STORAGE_SETTINGS["audio_base_path"])
    audio_chunks_path = os.getenv("AUDIO_CHUNKS_PATH", f"{audio_base_path}/audio_chunks")
    
    return {
        "audio_base_path": audio_base_path,
        "audio_chunks_path": audio_chunks_path,
    }


# Initialize settings on module load
_diarization_settings = load_diarization_settings_from_file()