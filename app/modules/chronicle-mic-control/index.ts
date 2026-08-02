// Local Expo module: iOS microphone-mode info and far-field input tuning.
// iOS-only — every function is a safe no-op on other platforms, and on dev
// clients built before this module existed (requireOptionalNativeModule
// returns null instead of throwing when the native side is missing).
import { requireOptionalNativeModule } from 'expo-modules-core';
import { Platform } from 'react-native';

export type MicrophoneMode = 'standard' | 'voiceIsolation' | 'wideSpectrum' | 'unknown';

export interface MicrophoneModeInfo {
  /** Mic Mode the user picked in Control Center (persists per app). */
  preferred: MicrophoneMode;
  /** Mic Mode actually in effect on the current audio route. */
  active: MicrophoneMode;
}

export interface FarFieldTuningResult {
  applied: boolean;
  dataSource?: string;
  polarPattern?: string;
  reason?: string;
}

interface ChronicleMicControlNative {
  getMicrophoneModeInfo(): Promise<MicrophoneModeInfo | null>;
  showMicrophoneModePicker(): Promise<boolean>;
  applyFarFieldTuning(): Promise<FarFieldTuningResult>;
}

const native =
  Platform.OS === 'ios'
    ? requireOptionalNativeModule<ChronicleMicControlNative>('ChronicleMicControl')
    : null;

/** Read the user's system Mic Mode. Returns null when unavailable (non-iOS, old build). */
export async function getMicrophoneModeInfo(): Promise<MicrophoneModeInfo | null> {
  if (!native) return null;
  try {
    return await native.getMicrophoneModeInfo();
  } catch {
    return null;
  }
}

/** Open the iOS system Mic Mode picker (apps cannot set the mode themselves). */
export async function showMicrophoneModePicker(): Promise<boolean> {
  if (!native) return false;
  try {
    return await native.showMicrophoneModePicker();
  } catch {
    return false;
  }
}

/** Prefer omnidirectional pickup on the built-in mic. Call after recording starts. */
export async function applyFarFieldTuning(): Promise<FarFieldTuningResult> {
  if (!native) return { applied: false, reason: 'native module unavailable' };
  try {
    return await native.applyFarFieldTuning();
  } catch (error) {
    return { applied: false, reason: (error as Error).message };
  }
}
