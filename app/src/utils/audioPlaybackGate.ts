export const DOWNLINK_PLAYBACK_ECHO_TAIL_MS = 350;

export interface DownlinkPlaybackCaptureGate {
  beginPlayback: (maximumDurationMs: number) => () => void;
  shouldSuppressCapture: () => boolean;
}

interface DownlinkPlaybackCaptureGateOptions {
  now?: () => number;
  tailMs?: number;
}

/**
 * Coordinates local speaker playback with microphone uplink.
 *
 * Recording and the WebSocket stay alive; only captured frames are withheld while
 * Chronicle is speaking. Deadline-bound leases handle overlapping replies without
 * letting a missing native completion event strand capture, and a short tail absorbs
 * the phone speaker/room reverberation after playback completes.
 *
 * This is intentionally a temporary half-duplex policy: it prevents self-echo but
 * cannot support barge-in until the capture path has reliable acoustic echo removal.
 */
export function createDownlinkPlaybackCaptureGate(
  options: DownlinkPlaybackCaptureGateOptions = {}
): DownlinkPlaybackCaptureGate {
  const now = options.now ?? Date.now;
  const tailMs = Math.max(0, options.tailMs ?? DOWNLINK_PLAYBACK_ECHO_TAIL_MS);
  const activePlaybacks = new Map<number, number>();
  let nextPlaybackId = 0;
  let suppressUntilMs = 0;

  const expireStalePlaybacks = () => {
    const currentTime = now();
    let latestExpiredAtMs = 0;

    for (const [playbackId, expiresAtMs] of activePlaybacks) {
      if (expiresAtMs > currentTime) continue;
      activePlaybacks.delete(playbackId);
      latestExpiredAtMs = Math.max(latestExpiredAtMs, expiresAtMs);
    }

    if (latestExpiredAtMs > 0 && activePlaybacks.size === 0) {
      suppressUntilMs = Math.max(suppressUntilMs, latestExpiredAtMs + tailMs);
    }
  };

  return {
    beginPlayback: (maximumDurationMs: number) => {
      if (!Number.isFinite(maximumDurationMs) || maximumDurationMs <= 0) {
        throw new Error('Playback capture suppression requires a positive finite deadline');
      }

      expireStalePlaybacks();
      const playbackId = ++nextPlaybackId;
      activePlaybacks.set(playbackId, now() + maximumDurationMs);
      let finished = false;

      return () => {
        if (finished) return;
        finished = true;

        expireStalePlaybacks();
        const removed = activePlaybacks.delete(playbackId);
        if (removed && activePlaybacks.size === 0) {
          suppressUntilMs = Math.max(suppressUntilMs, now() + tailMs);
        }
      };
    },
    shouldSuppressCapture: () => {
      expireStalePlaybacks();
      return activePlaybacks.size > 0 || now() < suppressUntilMs;
    },
  };
}

export const downlinkPlaybackCaptureGate = createDownlinkPlaybackCaptureGate();

export function shouldForwardCapturedAudio(
  byteLength: number,
  gate: DownlinkPlaybackCaptureGate = downlinkPlaybackCaptureGate
): boolean {
  return byteLength > 0 && !gate.shouldSuppressCapture();
}
