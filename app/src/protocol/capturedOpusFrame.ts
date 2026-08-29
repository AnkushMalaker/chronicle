import type { NativeOpusFrame } from '../../modules/chronicle-duplex-audio';

export interface CapturedOpusFrame {
  captureEpoch: number;
  capturedAtMs: number;
  monotonicTimestampMs: number;
  frameDurationMs: number;
  opus: Uint8Array;
}

export function capturedOpusFrameFromNative(
  frame: NativeOpusFrame,
  decodeBase64: (value: string) => Uint8Array
): CapturedOpusFrame {
  if (frame.sampleRate !== 16_000 || frame.channels !== 1) {
    throw new Error('native capture must emit 16 kHz mono Opus');
  }
  if (Math.abs(frame.frameDurationMs - 20) > 0.5) {
    throw new Error('native capture must emit 20 ms Opus packets');
  }
  const opus = decodeBase64(frame.opusBase64);
  if (!opus.length || opus.length > 1_275) {
    throw new Error('native capture emitted an invalid raw Opus packet');
  }
  return {
    captureEpoch: frame.captureEpoch,
    capturedAtMs: frame.capturedAtMs,
    monotonicTimestampMs: frame.monotonicTimestampMs,
    frameDurationMs: frame.frameDurationMs,
    opus,
  };
}
