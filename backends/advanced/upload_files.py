#!/usr/bin/env python3
"""
Upload audio files to the Chronicle backend for processing.
"""

import argparse
import logging
import os
import sys
import time
import wave
from pathlib import Path
from typing import Optional

import requests


# Configure colored logging
class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels."""
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'  # Reset color
    
    def format(self, record):
        # Add color to the log level
        if record.levelname in self.COLORS:
            record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
        return super().format(record)

# Configure logging with colors
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Apply colored formatter
for handler in logging.getLogger().handlers:
    handler.setFormatter(ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s'))

logger = logging.getLogger(__name__)


def load_env_variables() -> Optional[str]:
    """Load ADMIN_PASSWORD from .env file."""
    env_file = Path(".env")
    if not env_file.exists():
        logger.error(".env file not found. Please create it with ADMIN_PASSWORD.")
        return None
    
    admin_password = None
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('ADMIN_PASSWORD='):
                admin_password = line.split('=', 1)[1].strip('"\'')
                break
    
    if not admin_password:
        logger.error("ADMIN_PASSWORD not found in .env file.")
        return None
    
    return admin_password


def get_admin_token(password: str, base_url: str = "http://localhost:8000") -> Optional[str]:
    """Authenticate and get admin token."""
    logger.info("Requesting admin token...")
    
    auth_url = f"{base_url}/auth/jwt/login"
    
    try:
        response = requests.post(
            auth_url,
            data={
                'username': 'admin@example.com',
                'password': password
            },
            headers={
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            timeout=10
        )
        
        logger.info(f"Auth response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            token = data.get('access_token')
            if token:
                logger.info("Admin token obtained.")
                return token
            else:
                logger.error("No access token in response.")
                logger.error(f"Available fields: {list(data.keys())}")
                return None
        else:
            logger.error(f"Authentication failed with status {response.status_code}")
            try:
                error_data = response.json()
                logger.error(f"Error details: {error_data}")
            except Exception as json_error:
                logger.error(f"Failed to parse error response as JSON: {json_error}")
                logger.error(f"Response text: {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        return None


def get_audio_duration(file_path: str) -> float:
    """Get duration of WAV file in seconds using wave library."""
    try:
        with wave.open(file_path, "rb") as wav_file:
            frames = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
            duration = frames / sample_rate
            return duration
    except Exception as e:
        logger.warning(f"Could not determine audio duration for {file_path}: {e}")
        return 0.0


def validate_audio_format(file_path: str) -> tuple[bool, str]:
    """Validate that audio file is 16kHz, 16-bit mono format.
    
    Returns:
        tuple: (is_valid, error_message)
    """
    try:
        with wave.open(file_path, "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            
            errors = []
            
            if channels != 1:
                errors.append(f"Expected mono (1 channel), got {channels} channels")
            
            if sample_rate != 16000:
                errors.append(f"Expected 16kHz sample rate, got {sample_rate}Hz")
            
            if sample_width != 2:  # 2 bytes = 16 bits
                errors.append(f"Expected 16-bit audio, got {sample_width * 8}-bit")
            
            if errors:
                return False, "; ".join(errors)
            
            return True, ""
            
    except Exception as e:
        return False, f"Error reading WAV file: {e}"


def collect_wav_files(audio_dir: str, filter_list: Optional[list[str]] = None) -> list[str]:
    """Collect all .wav files from the specified directory with duration checking."""
    logger.info(f"Collecting .wav files from {audio_dir} ...")
    
    audio_path = Path(audio_dir).expanduser()
    if not audio_path.exists():
        logger.error(f"Directory {audio_path} does not exist.")
        return []
    
    wav_files = list(audio_path.glob("*.wav"))
    
    if not wav_files:
        logger.warning(f"No .wav files found in {audio_path}")
        return []
    
    # Filter files if filter_list is provided, otherwise accept all
    if filter_list is None:
        candidate_files = wav_files
    else:
        candidate_files = []
        for f in wav_files:
            if f.name in filter_list:
                candidate_files.append(f)
            else:
                logger.info(f"Skipping file (not in filter): {f.name}")
    
    # Check duration and filter out files over 20 minutes
    selected_files = []
    total_duration = 0.0
    
    for file_path in candidate_files:
        # First validate audio format
        is_valid, format_error = validate_audio_format(str(file_path))
        if not is_valid:
            logger.error(f"🔴 SKIPPING: {file_path.name} - Invalid format: {format_error}")
            continue
        
        duration = get_audio_duration(str(file_path))
        duration_minutes = duration / 60.0
        
        
        selected_files.append(file_path)
        total_duration += duration
        logger.info(f"✅ Added file: {file_path.name} (duration: {duration_minutes:.1f} minutes)")
    
    total_minutes = total_duration / 60.0
    logger.info(f"📊 Total files to upload: {len(selected_files)} (total duration: {total_minutes:.1f} minutes)")
    
    return [str(f) for f in selected_files]


def upload_files_async(files: list[str], token: str, base_url: str = "http://localhost:8000") -> bool:
    """Upload files to the backend for async processing with real-time progress tracking."""
    if not files:
        logger.error("No files to upload.")
        return False
    
    logger.info(f"🚀 Starting async upload to {base_url}/api/audio/upload ...")

    # Prepare files for upload
    files_data = []
    for file_path in files:
        try:
            files_data.append(('files', (os.path.basename(file_path), open(file_path, 'rb'), 'audio/wav')))
        except IOError as e:
            logger.error(f"Error opening file {file_path}: {e}")
            continue

    if not files_data:
        logger.error("No files could be opened for upload.")
        return False

    try:
        # Submit files for async processing
        response = requests.post(
            f"{base_url}/api/audio/upload",
            files=files_data,
            data={'device_name': 'file_upload_batch'},
            headers={
                'Authorization': f'Bearer {token}'
            },
            timeout=60  # Short timeout for job submission
        )
        
        # Close all file handles
        for _, file_tuple in files_data:
            file_tuple[1].close()
        
        if response.status_code != 200:
            logger.error(f"Failed to start async processing: {response.status_code}")
            try:
                error_data = response.json()
                logger.error(f"Error details: {error_data}")
            except:
                logger.error(f"Response text: {response.text}")
            return False
        
        # Get job ID
        job_data = response.json()
        job_id = job_data.get("job_id")
        total_files = job_data.get("total_files", 0)
        
        logger.info(f"✅ Job started successfully: {job_id}")
        logger.info(f"📊 Processing {total_files} files...")
        logger.info(f"🔗 Status URL: {job_data.get('status_url', 'N/A')}")
        
        # Poll for job completion
        return poll_job_status(job_id, token, base_url, total_files)
            
    except requests.exceptions.Timeout:
        logger.error("Job submission timed out.")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Job submission failed: {e}")
        return False
    finally:
        # Ensure all file handles are closed
        for _, file_tuple in files_data:
            try:
                file_tuple[1].close()
            except Exception as close_error:
                logger.warning(f"Failed to close file handle: {close_error}")


def poll_job_status(job_id: str, token: str, base_url: str, total_files: int) -> bool:
    """Poll job status until completion with progress updates."""
    status_url = f"{base_url}/api/queue/jobs/{job_id}"
    headers = {'Authorization': f'Bearer {token}'}
    
    start_time = time.time()
    last_progress = -1
    last_current_file = None
    
    logger.info("🔄 Polling job status...")
    
    while True:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)
            
            if response.status_code != 200:
                logger.error(f"Failed to get job status: {response.status_code}")
                return False
            
            job_status = response.json()
            status = job_status.get("status")
            progress = job_status.get("progress_percent", 0)
            current_file = job_status.get("current_file")
            processed_files = job_status.get("processed_files", 0)
            
            # Show progress updates
            if progress != last_progress or current_file != last_current_file:
                elapsed = time.time() - start_time
                if current_file:
                    logger.info(f"📈 Progress: {progress:.1f}% ({processed_files}/{total_files}) - Processing: {current_file} (elapsed: {elapsed:.0f}s)")
                else:
                    logger.info(f"📈 Progress: {progress:.1f}% ({processed_files}/{total_files}) (elapsed: {elapsed:.0f}s)")
                last_progress = progress
                last_current_file = current_file
            
            # Check completion status (RQ standard: "finished")
            if status == "finished":
                elapsed = time.time() - start_time
                logger.info(f"🎉 Job completed successfully in {elapsed:.0f}s!")

                # Show final file status summary
                files = job_status.get("files", [])
                completed = len([f for f in files if f.get("status") == "finished"])
                failed = len([f for f in files if f.get("status") == "failed"])
                skipped = len([f for f in files if f.get("status") == "skipped"])
                
                logger.info(f"📊 Final Summary:")
                logger.info(f"   ✅ Completed: {completed}")
                if failed > 0:
                    logger.error(f"   ❌ Failed: {failed}")
                if skipped > 0:
                    logger.warning(f"   ⏭️  Skipped: {skipped}")
                
                # Show failed files
                for file_info in files:
                    if file_info.get("status") == "failed":
                        error_msg = file_info.get("error_message", "Unknown error")
                        logger.error(f"   ❌ {file_info.get('filename')}: {error_msg}")
                    elif file_info.get("status") == "skipped":
                        error_msg = file_info.get("error_message", "Skipped")
                        logger.warning(f"   ⏭️  {file_info.get('filename')}: {error_msg}")
                
                return completed > 0  # Success if at least one file completed
                
            elif status == "failed":
                elapsed = time.time() - start_time
                error_msg = job_status.get("error_message", "Unknown error")
                logger.error(f"💥 Job failed after {elapsed:.0f}s: {error_msg}")
                return False
                
            elif status in ["queued", "processing"]:
                # Continue polling
                time.sleep(5)  # Poll every 5 seconds
                continue
            else:
                logger.warning(f"Unknown job status: {status}")
                time.sleep(5)
                continue
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Error polling job status: {e}")
            time.sleep(10)  # Wait longer on error
            continue
        except KeyboardInterrupt:
            logger.warning("Polling interrupted by user")
            return False


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Upload audio files to Chronicle backend")
    parser.add_argument(
        "files",
        nargs="*",
        help="Audio files to upload. If none provided, uses default test file."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Backend base URL (default: http://localhost:8000)"
    )
    return parser.parse_args()


def main():
    """Main function to orchestrate the upload process."""
    args = parse_args()
    
    logger.info("Chronicle Audio File Upload Tool")
    logger.info("=" * 40)
    
    # Load environment variables
    admin_password = load_env_variables()
    if not admin_password:
        sys.exit(1)
    
    # Get admin token
    token = get_admin_token(admin_password, args.base_url)
    if not token:
        sys.exit(1)
    
    # Determine files to upload
    if args.files:
        # Use provided files
        wav_files = []
        for file_path in args.files:
            file_path = Path(file_path).expanduser().resolve()
            if file_path.exists():
                wav_files.append(str(file_path))
                logger.info(f"Added file: {file_path}")
            else:
                logger.error(f"File not found: {file_path}")
                sys.exit(1)
    else:
        # Use default test file (committed to git, used in tests)
        project_root = Path(__file__).parent.parent.parent
        specific_file = project_root / "extras" / "test-audios" / "DIY Experts Glass Blowing_16khz_mono_4min.wav"
        
        if specific_file.exists():
            wav_files = [str(specific_file)]
            logger.info(f"Using default test file: {specific_file}")
        else:
            logger.error(f"Default test file not found: {specific_file}")
            logger.info("Please provide file paths as arguments or ensure test file exists")
            sys.exit(1)
    
    if not wav_files:
        logger.error("No files to upload")
        sys.exit(1)
    
    logger.info(f"Uploading {len(wav_files)} files:")
    for f in wav_files:
        logger.info(f"- {os.path.basename(f)}")
    
    success = upload_files_async(wav_files, token, args.base_url)
    
    if success:
        logger.info("🎉 Upload process completed successfully!")
        sys.exit(0)
    else:
        logger.error("💥 Upload process failed.")
        sys.exit(1)


if __name__ == "__main__":
    main() 