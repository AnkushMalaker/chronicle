import type { NativePcmFrame } from '../../modules/chronicle-duplex-audio';
import type { CapturedPcmFrame } from '../../../contracts/voice_protocol/v1/typescript/interactivePcm';

/** Preserve native capture clocks and format while decoding the transport payload. */
export function capturedPcmFrameFromNative(
  frame: NativePcmFrame,
  decode: (value: string) => Uint8Array,
): CapturedPcmFrame {
  return {
    captureEpoch: frame.captureEpoch,
    capturedAtMs: frame.capturedAtMs,
    monotonicTimestampMs: frame.monotonicTimestampMs,
    sampleRate: frame.sampleRate,
    channels: frame.channels,
    sampleWidth: frame.sampleWidth,
    pcm: decode(frame.pcmBase64),
  };
}
