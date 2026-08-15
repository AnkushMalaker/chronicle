// audioPlayback.ts
//
// Plays "downlink" audio that the backend pushes to a connected device over the
// audio WebSocket. This mirrors what the HAVPE relay does: the backend publishes
// a `play-audio` control frame to `device:downlink:{client_id}`, the WebSocket
// controller forwards it to the device, and the device plays the audio locally.
//
// On the phone the audio is delivered inline as base64-encoded WAV bytes
// (`{ type: "play-audio", data: { audio_b64, format } }`). We stage the bytes to a
// temp file in the cache directory and play them through the speaker with
// expo-audio.

import { createAudioPlayer, setAudioModeAsync, type AudioPlayer } from 'expo-audio';
import { File, Paths } from 'expo-file-system';
import { downlinkPlaybackCaptureGate } from './audioPlaybackGate';
import { logInfo, logWarn } from './logger';

export interface PlayAudioData {
  /** Base64-encoded audio bytes (e.g. a synthesized TTS reply). */
  audio_b64?: string;
  /** Optional URL to fetch audio from instead of inline bytes. */
  url?: string;
  /** Container format / file extension (defaults to "wav"). */
  format?: string;
}

// Configure the audio session once. We want playback to come out of the speaker
// while the microphone is simultaneously recording for the streaming session.
let audioModeConfigured = false;
async function ensureAudioMode(): Promise<void> {
  if (audioModeConfigured) return;
  try {
    await setAudioModeAsync({
      playsInSilentMode: true, // iOS: play even when the ringer switch is silenced
      allowsRecording: true, // iOS: keep mic capture working during playback
      shouldRouteThroughEarpiece: false, // Android: route to the loudspeaker
    });
    audioModeConfigured = true;
  } catch (e) {
    console.warn('[AudioPlayback] Failed to configure audio mode:', e);
  }
}

// Monotonic counter so concurrent/overlapping replies don't clobber each other's
// temp files before playback finishes.
let fileSeq = 0;
const MAX_DOWNLINK_PLAYBACK_MS = 2 * 60 * 1000;
const LOCAL_WAV_STARTUP_GRACE_MS = 250;

function readAscii(bytes: Uint8Array, offset: number, length: number): string {
  let value = '';
  for (let index = 0; index < length; index += 1) {
    value += String.fromCharCode(bytes[offset + index]);
  }
  return value;
}

function readUint32LE(bytes: Uint8Array, offset: number): number {
  return (
    bytes[offset]
    | (bytes[offset + 1] << 8)
    | (bytes[offset + 2] << 16)
    | (bytes[offset + 3] << 24)
  ) >>> 0;
}

/** Return the PCM/container duration from an ordinary RIFF/WAVE file. */
export function getWavDurationMs(bytes: Uint8Array): number | null {
  if (
    bytes.length < 12
    || readAscii(bytes, 0, 4) !== 'RIFF'
    || readAscii(bytes, 8, 4) !== 'WAVE'
  ) {
    return null;
  }

  let byteRate: number | null = null;
  let dataSize: number | null = null;
  let offset = 12;

  while (offset + 8 <= bytes.length) {
    const chunkId = readAscii(bytes, offset, 4);
    const chunkSize = readUint32LE(bytes, offset + 4);
    const chunkDataOffset = offset + 8;
    const chunkDataEnd = chunkDataOffset + chunkSize;
    if (chunkDataEnd > bytes.length) return null;

    if (chunkId === 'fmt ' && chunkSize >= 16) {
      byteRate = readUint32LE(bytes, chunkDataOffset + 8);
    } else if (chunkId === 'data') {
      dataSize = chunkSize;
    }

    if (byteRate !== null && dataSize !== null) break;
    offset = chunkDataEnd + (chunkSize % 2);
  }

  if (!byteRate || dataSize === null) return null;
  const durationMs = (dataSize / byteRate) * 1_000;
  return Number.isFinite(durationMs) && durationMs > 0 ? Math.ceil(durationMs) : null;
}

function isWavFormat(format: string): boolean {
  const normalized = format.trim().toLowerCase();
  return normalized === 'wav' || normalized === 'wave' || normalized === 'audio/wav';
}

/**
 * Play a backend `play-audio` downlink message through the phone speaker.
 * Fire-and-forget: errors are logged, never thrown.
 */
export async function playDownlinkAudio(data: PlayAudioData): Promise<void> {
  const { audio_b64, url, format = 'wav' } = data || {};

  let source: string | null = null;
  let tempFile: File | null = null;
  let measuredDurationMs: number | null = null;

  if (audio_b64) {
    try {
      const file = new File(Paths.cache, `downlink_${Date.now()}_${fileSeq++}.${format}`);
      if (file.exists) file.delete();
      file.write(audio_b64, { encoding: 'base64' });
      tempFile = file;
      source = file.uri;
      if (isWavFormat(format)) {
        measuredDurationMs = getWavDurationMs(file.bytesSync());
      }
    } catch (e) {
      console.warn('[AudioPlayback] Failed to stage downlink audio bytes:', e);
      return;
    }
  } else if (url) {
    source = url;
  } else {
    console.warn('[AudioPlayback] play-audio message had no audio_b64 or url');
    return;
  }

  await ensureAudioMode();

  let player: AudioPlayer;
  try {
    // expo-audio and expo-audio-studio share iOS's singleton AVAudioSession.
    // The player default deactivates that session when playback finishes, which
    // silently stops the separately-owned live recorder from emitting frames.
    player = createAudioPlayer(source, { keepAudioSessionActive: true });
  } catch (e) {
    console.warn('[AudioPlayback] Failed to create audio player:', e);
    if (tempFile) {
      try {
        tempFile.delete();
      } catch {}
    }
    return;
  }

  let cleanedUp = false;
  let finishCaptureSuppression: (() => void) | null = null;
  let playbackSafetyTimer: ReturnType<typeof setTimeout> | null = null;
  let statusSubscription: { remove: () => void } | null = null;
  const maximumSuppressionMs = measuredDurationMs === null
    ? MAX_DOWNLINK_PLAYBACK_MS
    : Math.min(MAX_DOWNLINK_PLAYBACK_MS, measuredDurationMs + LOCAL_WAV_STARTUP_GRACE_MS);

  const cleanup = (reason: string) => {
    if (cleanedUp) return;
    cleanedUp = true;
    if (playbackSafetyTimer) {
      clearTimeout(playbackSafetyTimer);
      playbackSafetyTimer = null;
    }
    try {
      statusSubscription?.remove();
    } catch {}
    statusSubscription = null;
    finishCaptureSuppression?.();
    finishCaptureSuppression = null;
    try {
      player.remove();
    } catch {}
    if (tempFile) {
      try {
        tempFile.delete();
      } catch {}
      tempFile = null;
    }
    logInfo(
      'AudioPlayback',
      `capture_gate_released reason=${reason} maximum_ms=${maximumSuppressionMs}`
    );
  };

  try {
    let sawPlayback = false;
    statusSubscription = player.addListener('playbackStatusUpdate', (status) => {
      if (status.playing) sawPlayback = true;
      if (status.didJustFinish) {
        cleanup('did_just_finish');
        return;
      }
      if (
        sawPlayback
        && !status.playing
        && status.duration > 0
        && status.currentTime >= status.duration - 0.05
      ) {
        cleanup('terminal_status');
      }
    });
    finishCaptureSuppression = downlinkPlaybackCaptureGate.beginPlayback(maximumSuppressionMs);
    playbackSafetyTimer = setTimeout(() => {
      const message = `capture_gate_release reason=duration_timeout maximum_ms=${maximumSuppressionMs}`;
      console.warn(`[AudioPlayback] ${message}`);
      logWarn('AudioPlayback', message);
      cleanup('duration_timeout');
    }, maximumSuppressionMs);
    logInfo(
      'AudioPlayback',
      `capture_gate_started format=${format} measured_ms=${measuredDurationMs ?? 'unknown'} maximum_ms=${maximumSuppressionMs}`
    );
    console.log(`[AudioPlayback] ▶️ Playing downlink audio (${format}) from ${source}`);
    player.play();
  } catch (e) {
    console.warn('[AudioPlayback] Failed to play downlink audio:', e);
    cleanup('play_error');
  }
}
