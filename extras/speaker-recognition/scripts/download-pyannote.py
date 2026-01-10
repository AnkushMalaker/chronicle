#!/usr/bin/env python3
"""
Script to download and cache PyAnnote models for speaker recognition.
This script is designed to be run during Docker build to pre-download models.
"""

import logging
import os
import sys

from pyannote.audio import Pipeline
from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def download_models():
    """Download and cache PyAnnote models."""
    try:
        # Set HuggingFace cache directory
        hf_home = os.environ.get('HF_HOME', '/models')
        os.environ['HF_HOME'] = hf_home
        
        logger.info(f"Downloading models to: {hf_home}")
        
        # Check if HF token is available
        hf_token = os.environ.get('HF_TOKEN')
        if not hf_token:
            logger.warning("No HF_TOKEN found. Models will be downloaded at runtime when token is available.")
            return True  # Don't fail the build, just skip download
        
        # Import and download models
        
        logger.info("Downloading speaker diarization model...")
        Pipeline.from_pretrained('pyannote/speaker-diarization-community-1', token=hf_token)
        
        logger.info("Downloading speaker embedding model...")
        PretrainedSpeakerEmbedding('pyannote/wespeaker-voxceleb-resnet34-LM', token=hf_token)
        
        logger.info("Models downloaded successfully!")
        return True
        
    except Exception as e:
        logger.warning(f"Failed to download models during build (will download at runtime): {e}")
        return True  # Don't fail the build

if __name__ == "__main__":
    success = download_models()
    sys.exit(0 if success else 1)
