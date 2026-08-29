import { useCallback, useEffect, useRef, useState } from 'react';
import { PermissionsAndroid, Platform } from 'react-native';
// @ts-ignore - no type declarations available
import base64 from 'react-native-base64';

import {
  addOpusFrameListener,
  startVoiceSession,
  stopVoiceSession,
} from '../../modules/chronicle-duplex-audio';
import type { VoiceCapabilities } from '../protocol/audioCapabilities';
import {
  capturedOpusFrameFromNative,
  type CapturedOpusFrame,
} from '../protocol/capturedOpusFrame';

export interface PhoneCaptureSession {
  captureEpoch: number;
  capabilities: VoiceCapabilities;
  restartCapture: () => Promise<PhoneCaptureSession>;
  stopCapture: () => Promise<void>;
}

interface UsePhoneAudioRecorder {
  isRecording: boolean;
  isInitializing: boolean;
  error: string | null;
  audioLevel: number;
  startRecording: (
    onAudioData: (frame: CapturedOpusFrame) => void
  ) => Promise<PhoneCaptureSession>;
  stopRecording: () => Promise<void>;
}

function decodeBase64(value: string): Uint8Array {
  const binary = base64.decode(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export const usePhoneAudioRecorder = (): UsePhoneAudioRecorder => {
  const [isRecording, setIsRecording] = useState(false);
  const [isInitializing, setIsInitializing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [audioLevel, setAudioLevel] = useState(0);
  const mountedRef = useRef(true);
  const captureEpochRef = useRef(0);
  const frameSubscriptionRef = useRef<{ remove: () => void } | null>(null);
  const onAudioDataRef = useRef<((frame: CapturedOpusFrame) => void) | null>(null);

  const markCaptureStopped = useCallback(() => {
    frameSubscriptionRef.current?.remove();
    frameSubscriptionRef.current = null;
    onAudioDataRef.current = null;
    if (mountedRef.current) {
      setIsRecording(false);
      setIsInitializing(false);
      setAudioLevel(0);
    }
  }, []);

  const stopRecording = useCallback(async () => {
    try {
      await stopVoiceSession();
    } finally {
      markCaptureStopped();
    }
  }, [markCaptureStopped]);

  const startNativeCapture = useCallback(async (): Promise<PhoneCaptureSession> => {
    const captureEpoch = captureEpochRef.current + 1;
    captureEpochRef.current = captureEpoch;
    const capabilities = await startVoiceSession({ captureEpoch });
    return {
      captureEpoch,
      capabilities,
      restartCapture: startNativeCapture,
      stopCapture: stopRecording,
    };
  }, [stopRecording]);

  const startRecording = useCallback(async (
    onAudioData: (frame: CapturedOpusFrame) => void
  ): Promise<PhoneCaptureSession> => {
    if (isRecording) await stopRecording();
    if (mountedRef.current) {
      setIsInitializing(true);
      setError(null);
    }
    if (Platform.OS === 'android') {
      const permission = await PermissionsAndroid.request(
        PermissionsAndroid.PERMISSIONS.RECORD_AUDIO
      );
      if (permission !== PermissionsAndroid.RESULTS.GRANTED) {
        throw new Error('Microphone permission denied');
      }
    }

    try {
      onAudioDataRef.current = onAudioData;
      frameSubscriptionRef.current = addOpusFrameListener((frame) => {
        if (!mountedRef.current || frame.captureEpoch !== captureEpochRef.current) return;
        const captured = capturedOpusFrameFromNative(frame, decodeBase64);
        if (!captured.opus.length) return;
        onAudioDataRef.current?.(captured);
      });
      const capture = await startNativeCapture();
      if (mountedRef.current) {
        setIsRecording(true);
        setIsInitializing(false);
      }
      return capture;
    } catch (cause) {
      frameSubscriptionRef.current?.remove();
      frameSubscriptionRef.current = null;
      const message = cause instanceof Error ? cause.message : 'Failed to start duplex audio';
      if (mountedRef.current) {
        setError(message);
        setIsInitializing(false);
        setIsRecording(false);
      }
      throw cause;
    }
  }, [isRecording, startNativeCapture, stopRecording]);

  useEffect(() => () => {
    mountedRef.current = false;
    frameSubscriptionRef.current?.remove();
    frameSubscriptionRef.current = null;
    stopVoiceSession().catch(() => undefined);
  }, []);

  return {
    isRecording,
    isInitializing,
    error,
    audioLevel,
    startRecording,
    stopRecording,
  };
};
