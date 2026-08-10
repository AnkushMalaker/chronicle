"""Peak normalization for review clips.

The clips a human is asked to judge are systematically quieter than the ones that
behave normally, so a review tool that plays them raw looks broken instead of
informative.
"""

import array
import io
import math
import wave

import pytest

from advanced_omi_backend.utils.audio_chunk_utils import normalize_wav_peak


def wav_of(samples: list[int], rate: int = 16000, width: int = 2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(width)
        sink.setframerate(rate)
        sink.writeframes(array.array("h" if width == 2 else "b", samples).tobytes())
    return out.getvalue()


def samples_of(wav: bytes) -> array.array:
    with wave.open(io.BytesIO(wav)) as source:
        data = array.array("h")
        data.frombytes(source.readframes(source.getnframes()))
    return data


def test_a_quiet_clip_is_boosted_towards_full_scale():
    quiet = [300 if i % 2 else -300 for i in range(1600)]

    boosted, gain = normalize_wav_peak(wav_of(quiet))

    assert gain == pytest.approx(20 * math.log10(0.89 * 32767 / 300), abs=0.2)
    assert max(abs(v) for v in samples_of(boosted)) > 28000


def test_the_reported_gain_matches_what_was_applied():
    """The number is shown to a listener, so it has to be the real one."""
    original = [1000 if i % 2 else -1000 for i in range(800)]

    boosted, gain = normalize_wav_peak(wav_of(original))

    ratio = max(abs(v) for v in samples_of(boosted)) / 1000
    assert gain == pytest.approx(20 * math.log10(ratio), abs=0.2)


def test_an_already_loud_clip_is_left_alone():
    loud = [32000 if i % 2 else -32000 for i in range(400)]

    result, gain = normalize_wav_peak(wav_of(loud))

    assert gain == 0.0
    assert result == wav_of(loud)


def test_digital_silence_is_returned_untouched():
    """Multiplying silence yields louder silence and a misleading gain figure."""
    silence = [0] * 1600

    result, gain = normalize_wav_peak(wav_of(silence))

    assert gain == 0.0
    assert result == wav_of(silence)


def test_boosting_never_clips_past_full_scale():
    ragged = [1, -1, 250, -250, 40, -40] * 200

    boosted, _ = normalize_wav_peak(wav_of(ragged))

    assert max(abs(v) for v in samples_of(boosted)) <= 32767


def test_format_is_preserved():
    boosted, _ = normalize_wav_peak(wav_of([500, -500] * 800, rate=8000))

    with wave.open(io.BytesIO(boosted)) as source:
        assert source.getframerate() == 8000
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getnframes() == 1600


def test_non_sixteen_bit_audio_is_passed_through():
    eight_bit = wav_of([10, -10] * 100, width=1)

    result, gain = normalize_wav_peak(eight_bit)

    assert gain == 0.0
    assert result == eight_bit


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
