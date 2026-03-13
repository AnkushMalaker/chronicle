#!/usr/bin/env python3
"""
Script to install PyTorch with CUDA support for speaker recognition.
This script is designed to be run during Docker build to install PyTorch with proper caching.
"""

import logging
import os
import subprocess
import sys

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def install_pytorch():
    """Install PyTorch with CUDA support."""
    try:
        # Get compute mode from environment
        compute_mode = os.environ.get("COMPUTE_MODE", "cpu")

        if compute_mode == "gpu":
            logger.info("Installing PyTorch with CUDA 11.8 support for GTX 1070...")

            # Install numpy first (PyAnnote requires NumPy 1.x, not 2.x)
            logger.info("Installing NumPy 1.x...")
            subprocess.run(["uv", "pip", "install", "--system", "numpy<2"], check=True)

            # Install PyTorch with CUDA 11.8 support
            logger.info("Installing PyTorch 2.2.0 with CUDA 11.8...")
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--system",
                    "torch==2.2.0+cu118",
                    "torchaudio==2.2.0+cu118",
                    "--index-url",
                    "https://download.pytorch.org/whl/cu118",
                ],
                check=True,
            )

            logger.info("PyTorch with CUDA installed successfully!")
        else:
            logger.info("CPU mode detected, PyTorch will be installed via uv sync")

        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install PyTorch: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error installing PyTorch: {e}")
        return False


if __name__ == "__main__":
    success = install_pytorch()
    sys.exit(0 if success else 1)
