import type { NativePlaybackState } from '../../modules/chronicle-duplex-audio';

export type PhoneVoiceStatusTone = 'muted' | 'accent' | 'warning' | 'success' | 'danger';

export interface PhoneVoiceStatus {
  label: string;
  tone: PhoneVoiceStatusTone;
}

export function phoneVoiceStatus(
  isRecording: boolean,
  playbackState: NativePlaybackState['state'] | null
): PhoneVoiceStatus | null {
  if (!isRecording) return null;

  switch (playbackState) {
    case 'started':
      return { label: 'Chronicle speaking', tone: 'accent' };
    case 'cancelled':
      return { label: 'Speech interrupted', tone: 'warning' };
    case 'done':
      return { label: 'Response complete', tone: 'success' };
    case 'failed':
      return { label: 'Playback failed', tone: 'danger' };
    default:
      return { label: 'Listening', tone: 'muted' };
  }
}
