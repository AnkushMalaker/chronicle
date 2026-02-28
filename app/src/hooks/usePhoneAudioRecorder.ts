// usePhoneAudioRecorder.ts
import { useState, useRef, useCallback, useEffect } from 'react';
import { Alert, Platform } from 'react-native';
import {
  useAudioRecorder,
  AudioRecording,
  AudioAnalysis,
  ExpoAudioStreamModule,
} from '@siteed/expo-audio-studio';
import type { AudioDataEvent } from '@siteed/expo-audio-studio';
// @ts-ignore - no type declarations available
import base64 from 'react-native-base64';


interface UsePhoneAudioRecorder {
  isRecording: boolean;
  isInitializing: boolean;
  error: string | null;
  audioLevel: number;
  startRecording: (onAudioData: (pcmBuffer: Uint8Array) => void) => Promise<void>;
  stopRecording: () => Promise<void>;
}

// Audio format constants matching backend expectations
const RECORDING_CONFIG = {
  sampleRate: 16000 as const,      // 16kHz for backend compatibility
  channels: 1 as const,            // Mono
  encoding: 'pcm_16bit' as const,  // 16-bit PCM
  interval: 100,                   // Send audio every 100ms
  intervalAnalysis: 100,           // Analysis every 100ms
};

export const usePhoneAudioRecorder = (): UsePhoneAudioRecorder => {
  const [isInitializing, setIsInitializing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [audioLevel, setAudioLevel] = useState<number>(0);

  const onAudioDataRef = useRef<((pcmBuffer: Uint8Array) => void) | null>(null);
  const mountedRef = useRef<boolean>(true);

  // Use the expo-audio-studio hook
  const {
    startRecording: startRecorderInternal,
    stopRecording: stopRecorderInternal,
    isRecording,
    pauseRecording,
    resumeRecording,
    analysisData,
  } = useAudioRecorder();

  // Convert AudioDataEvent to PCM buffer
  const processAudioDataEvent = useCallback((event: AudioDataEvent): Uint8Array | null => {
    try {
      const audioData = event.data;
      console.log('[PhoneAudioRecorder] processAudioDataEvent called, data type:', typeof audioData);

      if (typeof audioData === 'string') {
        // Base64 encoded data (native platforms) - decode using react-native-base64
        console.log('[PhoneAudioRecorder] Decoding Base64 string, length:', audioData.length);
        const binaryString = base64.decode(audioData);
        console.log('[PhoneAudioRecorder] Decoded to binary string, length:', binaryString.length);

        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i);
        }
        console.log('[PhoneAudioRecorder] Created Uint8Array, length:', bytes.length);
        return bytes;
      } else if (audioData instanceof Float32Array) {
        // Float32Array (web platform) - convert to 16-bit PCM
        const int16Buffer = new Int16Array(audioData.length);
        for (let i = 0; i < audioData.length; i++) {
          // Convert float32 (-1 to 1) to int16 (-32768 to 32767)
          const s = Math.max(-1, Math.min(1, audioData[i]));
          int16Buffer[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        // Convert Int16Array to Uint8Array (little-endian)
        const buffer = new ArrayBuffer(int16Buffer.length * 2);
        const view = new DataView(buffer);
        for (let i = 0; i < int16Buffer.length; i++) {
          view.setInt16(i * 2, int16Buffer[i], true); // little-endian
        }
        return new Uint8Array(buffer);
      }
      return null;
    } catch (error) {
      console.error('[PhoneAudioRecorder] Audio conversion error:', error);
      return null;
    }
  }, []);

  // Safe state setter
  const setStateSafe = useCallback(<T,>(setter: (v: T) => void, val: T) => {
    if (mountedRef.current) setter(val);
  }, []);

  // Check and request microphone permissions
  const checkPermissions = useCallback(async (): Promise<boolean> => {
    try {
      const { granted } = await ExpoAudioStreamModule.getPermissionsAsync();
      if (granted) {
        return true;
      }

      const { granted: newGranted } = await ExpoAudioStreamModule.requestPermissionsAsync();
      if (!newGranted) {
        Alert.alert(
          'Microphone Permission Required',
          'Please enable microphone access in your device settings to use phone audio streaming.',
          [{ text: 'OK' }]
        );
        return false;
      }
      return true;
    } catch (error) {
      console.error('[PhoneAudioRecorder] Permission check error:', error);
      return false;
    }
  }, []);

  // Start recording from phone microphone - EXACT 2025 guide pattern
  const startRecording = useCallback(async (onAudioData: (pcmBuffer: Uint8Array) => void): Promise<void> => {
    if (isRecording) {
      console.log('[PhoneAudioRecorder] Already recording, stopping first...');
      await stopRecording();
    }

    setStateSafe(setIsInitializing, true);
    setStateSafe(setError, null);
    onAudioDataRef.current = onAudioData;

    try {
      // EXACT permission check from guide
      const { granted } = await ExpoAudioStreamModule.requestPermissionsAsync();
      if (!granted) {
        throw new Error('Microphone permission denied');
      }

      console.log('[PhoneAudioRecorder] Starting audio recording...');

      // EXACT config from 2025 guide + processing for audio levels
      const config = {
        interval: 100,
        sampleRate: 16000 as const,
        channels: 1 as const,
        encoding: "pcm_16bit" as const,
        enableProcessing: true,        // Enable audio analysis for live RMS
        intervalAnalysis: 500,         // Analysis every 500ms
        onAudioStream: async (event: AudioDataEvent) => {
          // EXACT payload handling from guide
          const payload = typeof event.data === "string"
            ? event.data
            : Buffer.from(event.data as unknown as ArrayBuffer).toString("base64");

          // Convert to our expected format
          if (onAudioDataRef.current && mountedRef.current) {
            const pcmBuffer = processAudioDataEvent(event);
            if (pcmBuffer && pcmBuffer.length > 0) {
              onAudioDataRef.current(pcmBuffer);
            }
          }
        }
      };

      const result = await startRecorderInternal(config);

      if (!result) {
        throw new Error('Failed to start recording');
      }

      setStateSafe(setIsInitializing, false);
      console.log('[PhoneAudioRecorder] Recording started successfully');

    } catch (error) {
      const errorMessage = (error as any).message || 'Failed to start recording';
      console.error('[PhoneAudioRecorder] Start recording error:', errorMessage);
      setStateSafe(setError, errorMessage);
      setStateSafe(setIsInitializing, false);
      onAudioDataRef.current = null;

      throw new Error(errorMessage);
    }
  }, [isRecording, startRecorderInternal, processAudioDataEvent, setStateSafe]);

  // Stop recording
  const stopRecording = useCallback(async (): Promise<void> => {
    console.log('[PhoneAudioRecorder] Stopping recording...');

    // Early return if not recording
    if (!isRecording) {
      console.log('[PhoneAudioRecorder] Not recording, nothing to stop');
      onAudioDataRef.current = null;
      setStateSafe(setAudioLevel, 0);
      setStateSafe(setIsInitializing, false);
      return;
    }

    onAudioDataRef.current = null;
    setStateSafe(setAudioLevel, 0);

    try {
      const result = await stopRecorderInternal();
      console.log('[PhoneAudioRecorder] Recording stopped');
    } catch (error) {
      // Only log error if it's not about recording being inactive
      const errorMessage = (error as any).message || '';
      if (!errorMessage.includes('Recording is not active') && !errorMessage.includes('not active')) {
        console.error('[PhoneAudioRecorder] Stop recording error:', error);
        setStateSafe(setError, 'Failed to stop recording');
      } else {
        console.log('[PhoneAudioRecorder] Recording was already inactive');
      }
    }

    setStateSafe(setIsInitializing, false);
  }, [isRecording, stopRecorderInternal, setStateSafe]);

  // Update audio level from analysis data
  useEffect(() => {
    if (analysisData?.dataPoints && analysisData.dataPoints.length > 0 && mountedRef.current) {
      const latestDataPoint = analysisData.dataPoints[analysisData.dataPoints.length - 1];
      const liveRMS = latestDataPoint.rms;
      setStateSafe(setAudioLevel, liveRMS);
    }
  }, [analysisData, setStateSafe]);

  // Cleanup on unmount - NO dependencies so it only runs on true unmount
  useEffect(() => {
    return () => {
      mountedRef.current = false;
      console.log('[PhoneAudioRecorder] Component unmounting, setting mountedRef to false');
    };
  }, []); // Empty dependency array - only runs on mount/unmount

  // Separate effect for stopping recording when needed
  useEffect(() => {
    return () => {
      // Stop recording if active when dependencies change
      if (isRecording) {
        stopRecorderInternal().catch(err =>
          console.error('[PhoneAudioRecorder] Cleanup stop error:', err)
        );
      }
    };
  }, [isRecording, stopRecorderInternal]);

  return {
    isRecording,
    isInitializing,
    error,
    audioLevel,
    startRecording,
    stopRecording,
  };
};
