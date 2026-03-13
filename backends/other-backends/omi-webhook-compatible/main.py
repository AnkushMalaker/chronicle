import os
import wave
from datetime import datetime

import fastapi
import uvicorn
from fastapi import HTTPException, Request

app = fastapi.FastAPI()

# Directory to store audio files
AUDIO_DIR = "audio_recordings"

# Ensure the audio directory exists (it should, from the previous step)
os.makedirs(AUDIO_DIR, exist_ok=True)

# --- WAV File Parameters ---
# These are common defaults. Adjust them if your audio source has different specs.
# Number of channels (1 for mono, 2 for stereo)
N_CHANNELS = 1
# Sample width in bytes (1 for 8-bit, 2 for 16-bit)
SAMP_WIDTH = 2
# Frame rate or sampling rate (e.g., 16000 Hz, 44100 Hz)
FRAME_RATE = 16000
# Compression type (usually 'NONE' for PCM)
COMP_TYPE = "NONE"
# Compression name
COMP_NAME = "not compressed"
# --- End WAV File Parameters ---


@app.post("/webhook/audio_bytes")
async def receive_audio_bytes(request: Request):
    """
    Receives raw audio bytes and saves them as a WAV file.
    """
    try:
        audio_bytes = await request.body()

        if not audio_bytes:
            raise HTTPException(status_code=400, detail="No audio data received.")

        # Generate a unique filename using a timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = os.path.join(AUDIO_DIR, f"audio_{timestamp}.wav")

        # Write the audio bytes to a WAV file
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(N_CHANNELS)
            wf.setsampwidth(SAMP_WIDTH)
            wf.setframerate(FRAME_RATE)
            wf.writeframes(audio_bytes)

        return {
            "message": "Audio received and saved successfully",
            "filename": filename,
        }

    except HTTPException as e:
        # Re-raise HTTPExceptions to let FastAPI handle them
        raise e
    except Exception as e:
        print(f"Error processing audio: {e}")  # For server-side logging
        raise HTTPException(status_code=500, detail=f"Error processing audio: {str(e)}")


def main():
    """
    Main function to run the FastAPI server.
    """
    print(f"Starting Uvicorn server. Listening on http://0.0.0.0:8000")
    print(f"Audio files will be saved in: {os.path.abspath(AUDIO_DIR)}")
    print("POST audio bytes to http://0.0.0.0:8000/webhook/audio_bytes")

    # It's common to run uvicorn from the command line like:
    # uvicorn main:app --reload
    # However, for self-contained execution for this example, we can run it programmatically.
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
