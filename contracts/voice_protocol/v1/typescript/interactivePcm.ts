/** Strict outbound PCM framing shared by every protocol-v1 voice transport. */

export const MIN_INTERACTIVE_FRAME_DURATION_MS = 20;
export const MAX_INTERACTIVE_FRAME_DURATION_MS = 100;

export interface CapturedPcmFrame {
  captureEpoch: number;
  pcm: Uint8Array;
  sampleRate: number;
  channels: number;
  sampleWidth: number;
  /** Unix epoch milliseconds for the first sample in pcm. */
  capturedAtMs: number;
  /** Monotonic milliseconds for the first sample in pcm. */
  monotonicTimestampMs: number;
}

export interface InteractivePcmChunkHeader {
  type: 'audio-chunk';
  data: {
    rate: number;
    width: number;
    channels: number;
    time_basis: 'captured';
    frame_sequence: number;
    monotonic_offset_ms: number;
    captured_at_ms: number;
  };
  payload_length: number;
}

export interface EncodedInteractivePcmFrame {
  header: InteractivePcmChunkHeader;
  payload: Uint8Array;
}

function requirePositiveInteger(value: number, label: string): void {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${label} must be a positive integer`);
  }
}

/**
 * Owns the capture-epoch sequence and clock origin for interactive PCM.
 * Callers provide native capture clocks; callers never assemble wire metadata.
 */
export class InteractivePcmFrameEncoder {
  private captureEpoch: number;
  private nextSequence = 0;
  private monotonicOriginMs: number | null = null;
  private lastMonotonicTimestampMs: number | null = null;

  constructor(captureEpoch: number) {
    this.captureEpoch = this.validateCaptureEpoch(captureEpoch);
  }

  reset(captureEpoch: number): void {
    this.captureEpoch = this.validateCaptureEpoch(captureEpoch);
    this.nextSequence = 0;
    this.monotonicOriginMs = null;
    this.lastMonotonicTimestampMs = null;
  }

  encode(frame: CapturedPcmFrame): EncodedInteractivePcmFrame {
    if (frame.captureEpoch !== this.captureEpoch) {
      throw new Error('interactive PCM frame does not match the active capture epoch');
    }
    requirePositiveInteger(frame.sampleRate, 'sampleRate');
    requirePositiveInteger(frame.channels, 'channels');
    requirePositiveInteger(frame.sampleWidth, 'sampleWidth');
    if (!(frame.pcm instanceof Uint8Array) || frame.pcm.byteLength === 0) {
      throw new Error('interactive PCM payload must be non-empty');
    }
    const bytesPerSample = frame.channels * frame.sampleWidth;
    if (frame.pcm.byteLength % bytesPerSample !== 0) {
      throw new Error('interactive PCM payload must be sample-aligned');
    }
    const durationMs = frame.pcm.byteLength / bytesPerSample * 1000 / frame.sampleRate;
    if (
      durationMs < MIN_INTERACTIVE_FRAME_DURATION_MS
      || durationMs > MAX_INTERACTIVE_FRAME_DURATION_MS
    ) {
      throw new Error(
        `interactive PCM frames must be ${MIN_INTERACTIVE_FRAME_DURATION_MS}-${MAX_INTERACTIVE_FRAME_DURATION_MS} ms, got ${durationMs.toFixed(3)} ms`,
      );
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
      throw new Error('interactive PCM monotonic clock moved backwards');
    }

    if (this.monotonicOriginMs === null) {
      this.monotonicOriginMs = frame.monotonicTimestampMs;
    }
    const sequence = this.nextSequence;
    const monotonicOffsetMs = frame.monotonicTimestampMs - this.monotonicOriginMs;
    this.nextSequence += 1;
    this.lastMonotonicTimestampMs = frame.monotonicTimestampMs;

    return {
      header: {
        type: 'audio-chunk',
        data: {
          rate: frame.sampleRate,
          width: frame.sampleWidth,
          channels: frame.channels,
          time_basis: 'captured',
          frame_sequence: sequence,
          monotonic_offset_ms: monotonicOffsetMs,
          captured_at_ms: frame.capturedAtMs,
        },
        payload_length: frame.pcm.byteLength,
      },
      payload: frame.pcm,
    };
  }

  private validateCaptureEpoch(value: number): number {
    if (!Number.isInteger(value) || value < 0) {
      throw new Error('captureEpoch must be a non-negative integer');
    }
    return value;
  }
}

/** Pick the Web Audio power-of-two callback size closest to 40 ms. */
export function selectInteractivePcmBufferSize(sampleRate: number): number {
  requirePositiveInteger(sampleRate, 'sampleRate');
  const candidates = [256, 512, 1024, 2048, 4096, 8192, 16384];
  const valid = candidates.filter((size) => {
    const durationMs = size * 1000 / sampleRate;
    return durationMs >= MIN_INTERACTIVE_FRAME_DURATION_MS
      && durationMs <= MAX_INTERACTIVE_FRAME_DURATION_MS;
  });
  if (!valid.length) {
    throw new Error(`sampleRate ${sampleRate} cannot produce a 20-100 ms Web Audio frame`);
  }
  return valid.reduce((best, size) => (
    Math.abs(size * 1000 / sampleRate - 40)
      < Math.abs(best * 1000 / sampleRate - 40) ? size : best
  ));
}
