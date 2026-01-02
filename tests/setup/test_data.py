"""
Test data for Robot Framework tests
"""
import os

# Test Data
SAMPLE_CONVERSATIONS = [
    {
        "id": "conv_001",
        "transcript": "This is a test conversation about AI development.",
        "created_at": "2025-01-15T10:00:00Z"
    },
    {
        "id": "conv_002",
        "transcript": "Another test conversation discussing machine learning.",
        "created_at": "2025-01-15T11:00:00Z"
    }
]

SAMPLE_MEMORIES = [
    {
        "text": "User prefers AI discussions in the morning",
        "importance": 0.8
    },
    {
        "text": "User is interested in machine learning applications",
        "importance": 0.7
    }
]

# Construct path relative to the tests directory (works from any working directory)
_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_AUDIO_FILE = os.path.join(_tests_dir, "test_assets", "DIY_Experts_Glass_Blowing_16khz_mono_1min.wav")
TEST_DEVICE_NAME = "Robot-test-device"

# Expected content for transcript quality verification
EXPECTED_TRANSCRIPT = "glass blowing"

# Expected memories for DIY Glass Blowing audio (from Python integration test)
# Source: backends/advanced/tests/assets/expected_memories.json
EXPECTED_MEMORIES = [
    "Nick assists significantly in the glass blowing process",
    "Excitement and nervousness expressed during the process",
    "Furnace contains about 400 pounds of liquid glass",
    "Choice of color for the flower is light blue",
    "Caitlin is mentioned as a participant",
    "Class involves making a trumpet flower",
    "Gravity is used as a tool in glass blowing",
    "Nick did most of the turning during the demonstration",
    "The video is sponsored by Squarespace."
]

# Expected segment timestamps for DIY Glass Blowing audio (4-minute version, 500 chunks)
# These are the cropped audio timestamps after silence removal
# Updated 2025-01-22 based on actual test output with streaming websocket processing
EXPECTED_SEGMENT_TIMES = [
    {"start": 0.0, "end": 10.08},
    {"start": 10.28, "end": 20.255},
    {"start": 20.455, "end": 21.895},
    {"start": 22.095, "end": 23.615},
    {"start": 23.815, "end": 28.135},
    {"start": 28.335, "end": 43.08},
    {"start": 43.28, "end": 44.48},
    {"start": 44.68, "end": 46.76},
    {"start": 46.96, "end": 50.24},
]

# Tolerance for segment timestamp comparison (seconds)
# Increased tolerance to account for floating point precision in streaming timestamps
SEGMENT_TIME_TOLERANCE = 0.01

# Example: Alternative expected segment timestamps for different test audio
# You can create additional datasets for different audio files
EXPECTED_SEGMENT_TIMES_SHORT = [
    {"start": 0.0, "end": 5.0},
    {"start": 5.2, "end": 10.5},
    {"start": 10.7, "end": 15.3},
]
