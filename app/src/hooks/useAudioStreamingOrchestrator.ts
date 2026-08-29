import { useState, useCallback } from 'react';
import { Alert } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { AppSettings } from './useAppSettings';
import type { StreamStartConfig } from './useAudioStreamer';
import type { PhoneCaptureSession } from './usePhoneAudioRecorder';
import type { CapturedOpusFrame } from '../protocol/capturedOpusFrame';

interface OrchestratorParams {
  omiConnection: OmiConnection;
  deviceConnection: {
    connectedDeviceId: string | null;
  };
  audioStreamer: {
    isStreaming: boolean;
    startStreaming: (url: string, config?: StreamStartConfig) => Promise<void>;
    stopStreaming: () => Promise<void>;
    sendDurableAudio: (audioBytes: Uint8Array) => void;
    sendInteractiveFrame: (frame: CapturedOpusFrame) => void;
    getWebSocketReadyState: () => number | undefined;
  };
  phoneAudioRecorder: {
    isRecording: boolean;
    startRecording: (
      onData: (frame: CapturedOpusFrame) => Promise<void>
    ) => Promise<PhoneCaptureSession>;
    stopRecording: () => Promise<void>;
  };
  originalStartAudioListener: (onAudioData: (bytes: Uint8Array) => void) => Promise<void>;
  originalStopAudioListener: () => Promise<void>;
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
  settings,
}: OrchestratorParams): AudioOrchestrator => {
  const [isPhoneAudioMode, setIsPhoneAudioMode] = useState<boolean>(false);

  const buildWebSocketUrl = useCallback((baseUrl: string): string => {
    let url = baseUrl.trim();
    url = url.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
    const parsed = new URL(url);
    parsed.pathname = '/ws/audio';
    parsed.search = '';
    url = parsed.toString();

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
    const parsed = new URL(url);
    parsed.pathname = '/ws/audio';
    parsed.search = '';
    url = parsed.toString();

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
      await originalStartAudioListener(async (audioBytes) => {
        if (audioBytes.length > 0) {
          audioStreamer.sendDurableAudio(audioBytes);
        }
      });
      // BLE capture is independent of network availability. The durable spool above
      // keeps receiving while this connection attempt fails or reconnects.
      audioStreamer.startStreaming(finalUrl).catch((error) => {
        console.warn('[AudioOrchestrator] Initial WebSocket connection failed; buffering locally:', error);
      });
    } catch (error) {
      Alert.alert('Error', 'Could not start audio listening or streaming.');
      if (audioStreamer.isStreaming) audioStreamer.stopStreaming();
    }
  }, [originalStartAudioListener, audioStreamer, settings.webSocketUrl, omiConnection, deviceConnection.connectedDeviceId, buildWebSocketUrl]);

  const handleStopAudioListeningAndStreaming = useCallback(async () => {
    await originalStopAudioListener();
    await audioStreamer.stopStreaming();
  }, [originalStopAudioListener, audioStreamer]);

  const handleStartPhoneAudioStreaming = useCallback(async () => {
    if (!settings.webSocketUrl?.trim()) {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }

    try {
      const finalUrl = buildPhoneWebSocketUrl(settings.webSocketUrl);
      const capture = await phoneAudioRecorder.startRecording(async (frame) => {
        const wsReady = audioStreamer.getWebSocketReadyState();
        if (wsReady === WebSocket.OPEN && frame.opus.length > 0) {
          audioStreamer.sendInteractiveFrame(frame);
        }
      });
      await audioStreamer.startStreaming(finalUrl, { phoneVoice: capture });
      setIsPhoneAudioMode(true);
    } catch (error) {
      Alert.alert('Error', 'Could not start phone audio streaming.');
      if (audioStreamer.isStreaming) audioStreamer.stopStreaming();
      if (phoneAudioRecorder.isRecording) await phoneAudioRecorder.stopRecording();
      setIsPhoneAudioMode(false);
    }
  }, [audioStreamer, phoneAudioRecorder, settings.webSocketUrl, buildPhoneWebSocketUrl]);

  const handleStopPhoneAudioStreaming = useCallback(async () => {
    await audioStreamer.stopStreaming();
    await phoneAudioRecorder.stopRecording();
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
