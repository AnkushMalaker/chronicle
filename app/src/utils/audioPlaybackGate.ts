export const DOWNLINK_PLAYBACK_ECHO_TAIL_MS = 350;

export interface DownlinkPlaybackCaptureGate {
  beginPlayback: () => () => void;
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
 * Chronicle is speaking. A counter handles overlapping replies, and a short tail
 * absorbs the phone speaker/room reverberation after playback completes.
 */
export function createDownlinkPlaybackCaptureGate(
  options: DownlinkPlaybackCaptureGateOptions = {}
): DownlinkPlaybackCaptureGate {
  const now = options.now ?? Date.now;
  const tailMs = Math.max(0, options.tailMs ?? DOWNLINK_PLAYBACK_ECHO_TAIL_MS);
  let activePlaybacks = 0;
  let suppressUntilMs = 0;

  return {
    beginPlayback: () => {
      activePlaybacks += 1;
      let finished = false;

      return () => {
        if (finished) return;
        finished = true;
        activePlaybacks -= 1;
        if (activePlaybacks === 0) {
          suppressUntilMs = Math.max(suppressUntilMs, now() + tailMs);
        }
      };
    },
    shouldSuppressCapture: () => activePlaybacks > 0 || now() < suppressUntilMs,
  };
}

export const downlinkPlaybackCaptureGate = createDownlinkPlaybackCaptureGate();

export function shouldForwardCapturedAudio(
  byteLength: number,
  gate: DownlinkPlaybackCaptureGate = downlinkPlaybackCaptureGate
): boolean {
  return byteLength > 0 && !gate.shouldSuppressCapture();
}
