"""Timeout policy for long, chunked diarization requests."""

from advanced_omi_backend.speaker_recognition_client import SpeakerRecognitionClient


def test_long_chunked_diarization_has_enough_request_time():
    """A ten-hour corpus item must outlive the old ten-minute HTTP ceiling."""

    client = SpeakerRecognitionClient.__new__(SpeakerRecognitionClient)

    assert client.calculate_timeout(10 * 60 * 60) == 3600.0


def test_unknown_audio_duration_keeps_short_request_timeout():
    client = SpeakerRecognitionClient.__new__(SpeakerRecognitionClient)

    assert client.calculate_timeout(None) == 30.0


def test_short_known_duration_allows_gpu_queueing_delay():
    client = SpeakerRecognitionClient.__new__(SpeakerRecognitionClient)

    assert client.calculate_timeout(20.0) == 900.0
