import { useState, useCallback } from 'react';
import { Alert } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { AppSettings } from './useAppSettings';
import type { MicCaptureProfile } from '../utils/storage';

interface OrchestratorParams {
  omiConnection: OmiConnection;
  deviceConnection: {
    connectedDeviceId: string | null;
  };
  audioStreamer: {
    isStreaming: boolean;
    startStreaming: (url: string) => Promise<void>;
    stopStreaming: () => void;
    sendAudio: (audioBytes: Uint8Array) => void;
    getWebSocketReadyState: () => number | undefined;
  };
  phoneAudioRecorder: {
    isRecording: boolean;
    startRecording: (
      onData: (pcmBuffer: Uint8Array) => Promise<void>,
      options?: { deviceId?: string; captureProfile?: MicCaptureProfile }
    ) => Promise<void>;
    stopRecording: () => Promise<void>;
  };
  originalStartAudioListener: (onAudioData: (bytes: Uint8Array) => void) => Promise<void>;
  originalStopAudioListener: () => Promise<void>;
  /** Resolves which input device to record from (undefined = system default mic). */
  resolvePhoneInputDeviceId?: () => Promise<string | undefined>;
  /** iOS mic processing profile to record with (default 'far-field'). */
  phoneCaptureProfile?: MicCaptureProfile;
  settings: AppSettings;
}

export interface AudioOrchestrator {
  isPhoneAudioMode: boolean;
  setIsPhoneAudioMode: (mode: boolean) => void;
  handleStartAudioListeningAndStreaming: () => Promise<void>;
  handleStopAudioListeningAndStreaming: () => Promise<void>;
  handleTogglePhoneAudio: () => Promise<void>;
}

export const useAudioStreamingOrchestrator = ({
  omiConnection,
  deviceConnection,
  audioStreamer,
  phoneAudioRecorder,
  originalStartAudioListener,
  originalStopAudioListener,
  resolvePhoneInputDeviceId,
  phoneCaptureProfile,
  settings,
}: OrchestratorParams): AudioOrchestrator => {
  const [isPhoneAudioMode, setIsPhoneAudioMode] = useState<boolean>(false);

  const buildWebSocketUrl = useCallback((baseUrl: string): string => {
    let url = baseUrl.trim();
    url = url.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
    if (!url.includes('/ws')) url = url.replace(/\/$/, '') + '/ws';
    if (!url.includes('codec=')) {
      const sep = url.includes('?') ? '&' : '?';
      url = url + sep + 'codec=opus';
    }

    const isAdvanced = settings.jwtToken && settings.isAuthenticated;
    if (isAdvanced) {
      const params = new URLSearchParams();
      params.append('token', settings.jwtToken!);
      const deviceName = settings.userId?.trim() || 'phone';
      params.append('device_name', deviceName);
      const separator = url.includes('?') ? '&' : '?';
      url = `${url}${separator}${params.toString()}`;
    }
    return url;
  }, [settings.jwtToken, settings.isAuthenticated, settings.userId]);

  const buildPhoneWebSocketUrl = useCallback((baseUrl: string): string => {
    let url = baseUrl.trim();
    url = url.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
    if (!url.includes('/ws')) url = url.replace(/\/$/, '') + '/ws';
    if (!url.includes('codec=')) {
      const sep = url.includes('?') ? '&' : '?';
      url = url + sep + 'codec=pcm';
    }

    const isAdvanced = settings.jwtToken && settings.isAuthenticated;
    if (isAdvanced) {
      const params = new URLSearchParams();
      params.append('token', settings.jwtToken!);
      const deviceName = settings.userId?.trim() || 'phone-mic';
      params.append('device_name', deviceName);
      const separator = url.includes('?') ? '&' : '?';
      url = `${url}${separator}${params.toString()}`;
    }
    return url;
  }, [settings.jwtToken, settings.isAuthenticated, settings.userId]);

  const handleStartAudioListeningAndStreaming = useCallback(async () => {
    if (!settings.webSocketUrl?.trim()) {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }
    if (!omiConnection.isConnected() || !deviceConnection.connectedDeviceId) {
      Alert.alert('Device Not Connected', 'Please connect to an OMI device first.');
      return;
    }

    try {
      const finalUrl = buildWebSocketUrl(settings.webSocketUrl);
      await audioStreamer.startStreaming(finalUrl);
      await originalStartAudioListener(async (audioBytes) => {
        const wsReady = audioStreamer.getWebSocketReadyState();
        if (wsReady === WebSocket.OPEN && audioBytes.length > 0) {
          await audioStreamer.sendAudio(audioBytes);
        }
      });
    } catch (error) {
      Alert.alert('Error', 'Could not start audio listening or streaming.');
      if (audioStreamer.isStreaming) audioStreamer.stopStreaming();
    }
  }, [originalStartAudioListener, audioStreamer, settings.webSocketUrl, omiConnection, deviceConnection.connectedDeviceId, buildWebSocketUrl]);

  const handleStopAudioListeningAndStreaming = useCallback(async () => {
    await originalStopAudioListener();
    audioStreamer.stopStreaming();
  }, [originalStopAudioListener, audioStreamer]);

  const handleStartPhoneAudioStreaming = useCallback(async () => {
    if (!settings.webSocketUrl?.trim()) {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }

    try {
      const finalUrl = buildPhoneWebSocketUrl(settings.webSocketUrl);
      // Resolve the input device (auto-prefers Bluetooth headset mic) before recording.
      const deviceId = resolvePhoneInputDeviceId ? await resolvePhoneInputDeviceId() : undefined;
      await audioStreamer.startStreaming(finalUrl);
      await phoneAudioRecorder.startRecording(async (pcmBuffer) => {
        const wsReady = audioStreamer.getWebSocketReadyState();
        if (wsReady === WebSocket.OPEN && pcmBuffer.length > 0) {
          await audioStreamer.sendAudio(pcmBuffer);
        }
      }, { deviceId, captureProfile: phoneCaptureProfile });
      setIsPhoneAudioMode(true);
    } catch (error) {
      Alert.alert('Error', 'Could not start phone audio streaming.');
      if (audioStreamer.isStreaming) audioStreamer.stopStreaming();
      if (phoneAudioRecorder.isRecording) await phoneAudioRecorder.stopRecording();
      setIsPhoneAudioMode(false);
    }
  }, [audioStreamer, phoneAudioRecorder, settings.webSocketUrl, buildPhoneWebSocketUrl, resolvePhoneInputDeviceId, phoneCaptureProfile]);

  const handleStopPhoneAudioStreaming = useCallback(async () => {
    await phoneAudioRecorder.stopRecording();
    audioStreamer.stopStreaming();
    setIsPhoneAudioMode(false);
  }, [phoneAudioRecorder, audioStreamer]);

  const handleTogglePhoneAudio = useCallback(async () => {
    if (isPhoneAudioMode || phoneAudioRecorder.isRecording) {
      await handleStopPhoneAudioStreaming();
    } else {
      await handleStartPhoneAudioStreaming();
    }
  }, [isPhoneAudioMode, phoneAudioRecorder.isRecording, handleStartPhoneAudioStreaming, handleStopPhoneAudioStreaming]);

  return {
    isPhoneAudioMode,
    setIsPhoneAudioMode,
    handleStartAudioListeningAndStreaming,
    handleStopAudioListeningAndStreaming,
    handleTogglePhoneAudio,
  };
};
