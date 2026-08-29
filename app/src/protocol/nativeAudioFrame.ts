import type { NativeAudioFrame } from '../../modules/chronicle-duplex-audio';
import type { CapturedAudioFrame } from '../../../contracts/voice_protocol/v1/typescript/interactiveAudio';

/** Preserve native capture clocks and codec facts while decoding the bridge payload. */
export function capturedAudioFrameFromNative(
  frame: NativeAudioFrame,
  decode: (value: string) => Uint8Array,
): CapturedAudioFrame {
  return {
    captureEpoch: frame.captureEpoch,
    capturedAtMs: frame.capturedAtMs,
    monotonicTimestampMs: frame.monotonicTimestampMs,
    codec: frame.codec,
    sampleRate: frame.sampleRate,
    channels: frame.channels,
    frameDurationMs: frame.frameDurationMs,
    audioLevel: frame.audioLevel,
    payload: decode(frame.payloadBase64),
  };
}
