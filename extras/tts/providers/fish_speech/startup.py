"""
Fish Speech startup orchestrator.

Handles the full startup sequence:
1. Download model from HuggingFace
2. Start fish-speech's native API server (background)
3. Start our FastAPI wrapper service (foreground)

This runs inside the container with `uv run` so fish-speech's venv is active.
"""

import logging
import os
import subprocess
import sys
import time

import requests
from huggingface_hub import snapshot_download

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("fish-tts-startup")

# Fish-speech internal API
_FISH_API_HOST = "0.0.0.0"
_FISH_API_PORT = 8080
_FISH_LISTEN_HOST = "127.0.0.1"

# Our wrapper service
_WRAPPER_PORT = int(os.getenv("TTS_PORT_INTERNAL", "8770"))


def download_model() -> str:
    """Download model from HuggingFace and return checkpoint path."""
    model_id = os.getenv("TTS_MODEL", "fishaudio/s2-pro")
    cache_dir = os.getenv("HF_HOME", "/models")

    logger.info(f"Downloading model: {model_id}")
    checkpoint_path = snapshot_download(model_id, cache_dir=cache_dir)
    logger.info(f"Model downloaded to: {checkpoint_path}")
    return checkpoint_path


def find_codec(checkpoint_path: str) -> str:
    """Locate codec.pth in the checkpoint directory."""
    codec_path = os.path.join(checkpoint_path, "codec.pth")
    if os.path.exists(codec_path):
        return codec_path

    for root, _dirs, files in os.walk(checkpoint_path):
        if "codec.pth" in files:
            return os.path.join(root, "codec.pth")

    raise FileNotFoundError(f"codec.pth not found in {checkpoint_path}")


def start_fish_server(checkpoint_path: str, codec_path: str) -> subprocess.Popen:
    """Start fish-speech's native API server as a background process."""
    decoder_config = os.getenv("TTS_DECODER_CONFIG", "modded_dac_vq")
    use_half = os.getenv("TTS_HALF", "true").lower() == "true"
    use_compile = os.getenv("TTS_COMPILE", "false").lower() == "true"

    cmd = [
        "uv", "run", "python", "-m", "tools.api_server",
        "--listen", f"{_FISH_LISTEN_HOST}:{_FISH_API_PORT}",
        "--llama-checkpoint-path", checkpoint_path,
        "--decoder-checkpoint-path", codec_path,
        "--decoder-config-name", decoder_config,
    ]
    if use_half:
        cmd.append("--half")
    if use_compile:
        cmd.append("--compile")

    logger.info(f"Starting fish-speech API server: {' '.join(cmd)}")
    process = subprocess.Popen(cmd)
    return process


def wait_for_server(process: subprocess.Popen, timeout: int = 300) -> None:
    """Wait for fish-speech server to be ready."""
    base_url = f"http://{_FISH_LISTEN_HOST}:{_FISH_API_PORT}"
    deadline = time.time() + timeout

    logger.info(f"Waiting for fish-speech server at {base_url} (timeout: {timeout}s)")

    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/v1/health", timeout=2)
            if resp.status_code == 200:
                logger.info("Fish-speech API server is ready")
                return
        except requests.ConnectionError:
            pass

        # Check if process died
        if process.poll() is not None:
            raise RuntimeError(
                f"Fish-speech server exited with code {process.returncode}"
            )
        time.sleep(2)

    raise RuntimeError(f"Fish-speech server failed to start within {timeout} seconds")


def start_wrapper() -> None:
    """Start our FastAPI wrapper service (foreground, blocks)."""
    # Add chronicle/ to Python path so imports like `from common.base_service`
    # and `from providers.fish_speech.service` resolve correctly
    chronicle_dir = os.path.join(os.path.dirname(__file__), "..", "..")
    chronicle_dir = os.path.abspath(chronicle_dir)
    sys.path.insert(0, chronicle_dir)

    import uvicorn

    from common.base_service import create_tts_app
    from providers.fish_speech.service import FishSpeechService

    model_id = os.getenv("TTS_MODEL", "fishaudio/s2-pro")
    service = FishSpeechService(model_id)
    app = create_tts_app(service)

    logger.info(f"Starting wrapper service on port {_WRAPPER_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=_WRAPPER_PORT)


def main() -> None:
    """Main startup sequence."""
    try:
        # Step 1: Download model
        checkpoint_path = download_model()

        # Step 2: Find codec
        codec_path = find_codec(checkpoint_path)
        logger.info(f"Found codec at: {codec_path}")

        # Step 3: Start fish-speech server
        fish_process = start_fish_server(checkpoint_path, codec_path)

        # Step 4: Wait for server readiness
        wait_for_server(fish_process)

        # Step 5: Start our wrapper (foreground, blocks)
        start_wrapper()

    except Exception:
        logger.exception("Startup failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
