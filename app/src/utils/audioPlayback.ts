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

/**
 * Play a backend `play-audio` downlink message through the phone speaker.
 * Fire-and-forget: errors are logged, never thrown.
 */
export async function playDownlinkAudio(data: PlayAudioData): Promise<void> {
  const { audio_b64, url, format = 'wav' } = data || {};

  let source: string | null = null;
  let tempFile: File | null = null;

  if (audio_b64) {
    try {
      const file = new File(Paths.cache, `downlink_${Date.now()}_${fileSeq++}.${format}`);
      if (file.exists) file.delete();
      file.write(audio_b64, { encoding: 'base64' });
      tempFile = file;
      source = file.uri;
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
    player = createAudioPlayer(source);
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

  const cleanup = () => {
    if (cleanedUp) return;
    cleanedUp = true;
    if (playbackSafetyTimer) {
      clearTimeout(playbackSafetyTimer);
      playbackSafetyTimer = null;
    }
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
  };

  try {
    const sub = player.addListener('playbackStatusUpdate', (status) => {
      if (status.didJustFinish) {
        try {
          sub.remove();
        } catch {}
        cleanup();
      }
    });
    finishCaptureSuppression = downlinkPlaybackCaptureGate.beginPlayback();
    playbackSafetyTimer = setTimeout(() => {
      console.warn('[AudioPlayback] Playback completion timed out; releasing capture gate');
      try {
        sub.remove();
      } catch {}
      cleanup();
    }, MAX_DOWNLINK_PLAYBACK_MS);
    console.log(`[AudioPlayback] ▶️ Playing downlink audio (${format}) from ${source}`);
    player.play();
  } catch (e) {
    console.warn('[AudioPlayback] Failed to play downlink audio:', e);
    cleanup();
  }
}
