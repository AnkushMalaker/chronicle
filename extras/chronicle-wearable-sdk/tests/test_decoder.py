from chronicle_wearable_sdk.decoder import OmiOpusDecoder
from opuslib import Decoder, Encoder


def _silence_packet() -> bytes:
    return Encoder(16_000, 1, "audio").encode(bytes(640), 320)


def test_three_byte_raw_opus_silence_is_not_mistaken_for_a_header():
    packet = _silence_packet()
    assert len(packet) == 3

    pcm = OmiOpusDecoder().decode_packet(packet, strip_header=False)

    assert len(pcm) == 640


def test_wearable_header_is_removed_only_when_requested():
    packet = _silence_packet()

    pcm = OmiOpusDecoder().decode_packet(b"\x00\x00\x00" + packet, strip_header=True)

    assert len(pcm) == 640
