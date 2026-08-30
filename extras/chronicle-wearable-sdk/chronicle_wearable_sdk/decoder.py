from opuslib import Decoder


class OmiOpusDecoder:
    def __init__(self):
        self.decoder = Decoder(16000, 1)  # 16kHz mono

    def decode_packet(self, data: bytes, strip_header: bool = True):
        if strip_header:
            if len(data) <= 3:
                return b""
            # OMI/Neo transport packets carry a three-byte device-local header.
            clean_data = bytes(data[3:])
        else:
            # Raw Opus silence is legitimately only three bytes. Length cannot be
            # used as a wearable-header heuristic at the typed Audio V2 boundary.
            if not data:
                return b""
            clean_data = data

        # Decode Opus to PCM 16-bit
        try:
            pcm = self.decoder.decode(clean_data, 960, decode_fec=False)
            return pcm
        except Exception as e:
            print("Opus decode error:", e)
            return b""
