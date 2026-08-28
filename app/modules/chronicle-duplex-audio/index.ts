import {
  type EventSubscription,
  type NativeModule,
  requireOptionalNativeModule,
} from 'expo-modules-core';
import { Platform } from 'react-native';

import type { VoiceCapabilities } from '../../src/protocol/voiceProtocol';

export interface StartVoiceSessionOptions {
  captureEpoch: number;
}

export interface NativePcmFrame {
  captureEpoch: number;
  capturedAtMs: number;
  monotonicTimestampMs: number;
  sampleRate: 16000;
  channels: 1;
  sampleWidth: 2;
  pcmBase64: string;
}

export interface NativeResponse {
  responseId: string;
  generation: number;
  captureEpoch: number;
  wavBase64: string;
}

export interface NativePlaybackState {
  responseId: string;
  generation: number;
  captureEpoch: number;
  state: 'started' | 'done' | 'cancelled' | 'failed';
  monotonicTimestampMs: number;
  errorCode: 'decode_failed' | 'route_changed' | 'engine_reset' | 'playback_unavailable' | null;
}

export interface NativeRouteChange {
  captureEpoch: number;
  reason: 'route_changed' | 'interruption' | 'engine_reset' | 'effect_failed' | 'audio_focus_lost';
  capabilities: VoiceCapabilities;
}

export interface NativeStopResult {
  restorationSucceeded: boolean;
  failureCode: 'far_field_restore_failed' | 'permission_denied' | 'engine_unavailable' | null;
}

type ChronicleDuplexAudioNative = NativeModule & {
  startVoiceSession(options: StartVoiceSessionOptions): Promise<VoiceCapabilities>;
  scheduleResponse(response: NativeResponse): Promise<void>;
  cancelResponse(responseId: string, generation: number): Promise<void>;
  stopVoiceSession(): Promise<NativeStopResult>;
  addListener(
    eventName: 'onPcmFrame',
    listener: (event: NativePcmFrame) => void
  ): EventSubscription;
  addListener(
    eventName: 'onPlaybackState',
    listener: (event: NativePlaybackState) => void
  ): EventSubscription;
  addListener(
    eventName: 'onRouteChange',
    listener: (event: NativeRouteChange) => void
  ): EventSubscription;
};

const native =
  Platform.OS === 'ios' || Platform.OS === 'android'
    ? requireOptionalNativeModule<ChronicleDuplexAudioNative>('ChronicleDuplexAudio')
    : null;

function requireNative(): ChronicleDuplexAudioNative {
  if (!native) {
    throw new Error('server_upgrade_required: Chronicle duplex native module unavailable');
  }
  if (Platform.OS === 'android' && Number(Platform.Version) < 31) {
    throw new Error('platform_unavailable: Chronicle duplex audio requires Android API 31');
  }
  return native;
}

export function startVoiceSession(
  options: StartVoiceSessionOptions
): Promise<VoiceCapabilities> {
  return requireNative().startVoiceSession(options);
}

export function addPcmFrameListener(
  listener: (event: NativePcmFrame) => void
): EventSubscription {
  return requireNative().addListener('onPcmFrame', listener);
}

export function addPlaybackStateListener(
  listener: (event: NativePlaybackState) => void
): EventSubscription {
  return requireNative().addListener('onPlaybackState', listener);
}

export function addRouteChangeListener(
  listener: (event: NativeRouteChange) => void
): EventSubscription {
  return requireNative().addListener('onRouteChange', listener);
}

export function scheduleResponse(response: NativeResponse): Promise<void> {
  return requireNative().scheduleResponse(response);
}

export function cancelResponse(responseId: string, generation: number): Promise<void> {
  return requireNative().cancelResponse(responseId, generation);
}

export function stopVoiceSession(): Promise<NativeStopResult> {
  return requireNative().stopVoiceSession();
}
