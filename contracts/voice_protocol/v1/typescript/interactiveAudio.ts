/** Strict outbound Opus framing shared by every protocol-v1 voice transport. */

export const INTERACTIVE_OPUS_SAMPLE_RATE = 16_000;
export const INTERACTIVE_OPUS_CHANNELS = 1;
export const INTERACTIVE_OPUS_FRAME_DURATIONS_MS = [20, 40, 60] as const;
export const MAX_RAW_OPUS_PACKET_BYTES = 1_275;

export interface CapturedAudioFrame {
  captureEpoch: number;
  codec: 'opus';
  payload: Uint8Array;
  sampleRate: typeof INTERACTIVE_OPUS_SAMPLE_RATE;
  channels: typeof INTERACTIVE_OPUS_CHANNELS;
  frameDurationMs: (typeof INTERACTIVE_OPUS_FRAME_DURATIONS_MS)[number];
  /** Unix epoch milliseconds for the first encoded sample. */
  capturedAtMs: number;
  /** Monotonic milliseconds for the first encoded sample. */
  monotonicTimestampMs: number;
  /** Native RMS after voice processing, before Opus encoding. */
  audioLevel: number;
}

export interface InteractiveAudioChunkHeader {
  type: 'audio-chunk';
  data: {
    codec: 'opus';
    rate: typeof INTERACTIVE_OPUS_SAMPLE_RATE;
    channels: typeof INTERACTIVE_OPUS_CHANNELS;
    frame_duration_ms: (typeof INTERACTIVE_OPUS_FRAME_DURATIONS_MS)[number];
    time_basis: 'captured';
    frame_sequence: number;
    monotonic_offset_ms: number;
    captured_at_ms: number;
  };
  payload_length: number;
}

export interface EncodedInteractiveAudioFrame {
  header: InteractiveAudioChunkHeader;
  payload: Uint8Array;
}

/**
 * Owns per-epoch sequencing and captured-clock wire metadata. Native adapters
 * provide one raw Opus packet; callers never assemble transport headers.
 */
export class InteractiveAudioFrameEncoder {
  private readonly captureEpoch: number;
  private nextSequence = 0;
  private monotonicOriginMs: number | null = null;
  private lastMonotonicTimestampMs: number | null = null;

  constructor(captureEpoch: number) {
    if (!Number.isInteger(captureEpoch) || captureEpoch < 0) {
      throw new Error('captureEpoch must be a non-negative integer');
    }
    this.captureEpoch = captureEpoch;
  }

  encode(frame: CapturedAudioFrame): EncodedInteractiveAudioFrame {
    if (frame.captureEpoch !== this.captureEpoch) {
      throw new Error('interactive audio frame does not match the active capture epoch');
    }
    if (frame.codec !== 'opus') {
      throw new Error('interactive audio codec must be opus');
    }
    if (frame.sampleRate !== INTERACTIVE_OPUS_SAMPLE_RATE || frame.channels !== 1) {
      throw new Error('interactive Opus must be 16 kHz mono');
    }
    if (!INTERACTIVE_OPUS_FRAME_DURATIONS_MS.includes(frame.frameDurationMs)) {
      throw new Error('interactive Opus frame duration must be 20, 40, or 60 ms');
    }
    if (!(frame.payload instanceof Uint8Array) || frame.payload.byteLength === 0) {
      throw new Error('interactive Opus payload must be non-empty');
    }
    if (frame.payload.byteLength > MAX_RAW_OPUS_PACKET_BYTES) {
      throw new Error('interactive Opus payload must be one raw packet');
    }
    if (
      frame.payload.byteLength >= 4
      && String.fromCharCode(...frame.payload.subarray(0, 4)) === 'OggS'
    ) {
      throw new Error('interactive Opus payload must not be an Ogg container');
    }
    if (!Number.isFinite(frame.capturedAtMs) || frame.capturedAtMs <= 0) {
      throw new Error('capturedAtMs must be a positive timestamp');
    }
    if (!Number.isFinite(frame.monotonicTimestampMs) || frame.monotonicTimestampMs < 0) {
      throw new Error('monotonicTimestampMs must be non-negative');
    }
    if (
      this.lastMonotonicTimestampMs !== null
      && frame.monotonicTimestampMs < this.lastMonotonicTimestampMs
    ) {
      throw new Error('interactive audio monotonic clock moved backwards');
    }

    if (this.monotonicOriginMs === null) {
      this.monotonicOriginMs = frame.monotonicTimestampMs;
    }
    const frameSequence = this.nextSequence;
    const monotonicOffsetMs = frame.monotonicTimestampMs - this.monotonicOriginMs;
    this.nextSequence += 1;
    this.lastMonotonicTimestampMs = frame.monotonicTimestampMs;

    return {
      header: {
        type: 'audio-chunk',
        data: {
          codec: 'opus',
          rate: INTERACTIVE_OPUS_SAMPLE_RATE,
          channels: INTERACTIVE_OPUS_CHANNELS,
          frame_duration_ms: frame.frameDurationMs,
          time_basis: 'captured',
          frame_sequence: frameSequence,
          monotonic_offset_ms: monotonicOffsetMs,
          captured_at_ms: frame.capturedAtMs,
        },
        payload_length: frame.payload.byteLength,
      },
      payload: frame.payload,
    };
  }
}
