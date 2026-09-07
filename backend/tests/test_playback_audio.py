import io
import wave

import opuslib

from backend.services.playback_audio import encode_wav_for_playback


def _wav(*, sample_rate: int, channels: int, frames: int) -> bytes:
    body = io.BytesIO()
    with wave.open(body, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(frames * channels * 2))
    return body.getvalue()


def test_wav_is_normalized_to_decodable_24khz_20ms_raw_opus_packets():
    encoded = encode_wav_for_playback(
        _wav(sample_rate=16_000, channels=2, frames=1_600)
    )
    decoder = opuslib.Decoder(24_000, 1)

    assert encoded.duration_ms == 100
    assert len(encoded.packets) == 5
    assert all(not packet.startswith(b"OggS") for packet in encoded.packets)
    assert all(len(decoder.decode(packet, 480)) == 960 for packet in encoded.packets)
