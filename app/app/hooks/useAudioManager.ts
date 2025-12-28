import { useState, useCallback, useRef, useEffect } from 'react';
import { Alert } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { UseOfflineModeReturn } from './useOfflineMode';

// Type definitions for audio streaming services
interface AudioStreamer {
  isStreaming: boolean;
  isConnecting: boolean;
  error: string | null;
  startStreaming: (url: string) => Promise<void>;
  stopStreaming: () => void;
  sendAudio: (data: Uint8Array) => Promise<void>;
  getWebSocketReadyState: () => number;
}

interface PhoneAudioRecorder {
  isRecording: boolean;
  isInitializing: boolean;
  error: string | null;
  audioLevel: number;
  startRecording: (onAudioData: (pcmBuffer: Uint8Array) => Promise<void>) => Promise<void>;
  stopRecording: () => Promise<void>;
}

// Callback types for connection events
interface ConnectionEventHandlers {
  onWebSocketDisconnect?: (sessionId: string, conversationId: string | null) => void;
  onWebSocketReconnect?: () => void;
}

interface UseAudioManagerParams {
  webSocketUrl: string;
  userId: string;
  jwtToken: string | null;
  isAuthenticated: boolean;
  omiConnection: OmiConnection;
  connectedDeviceId: string | null;
  audioStreamer: AudioStreamer;
  phoneAudioRecorder: PhoneAudioRecorder;
  startAudioListener: (onAudioData: (audioBytes: Uint8Array) => Promise<void>) => Promise<void>;
  stopAudioListener: () => Promise<void>;
  // Offline mode integration
  offlineMode?: UseOfflineModeReturn;
  connectionHandlers?: ConnectionEventHandlers;
}

interface UseAudioManagerReturn {
  isPhoneAudioMode: boolean;
  isOfflineBuffering: boolean;
  currentSessionId: string | null;
  currentConversationId: string | null;
  startOmiAudioStreaming: () => Promise<void>;
  stopOmiAudioStreaming: () => Promise<void>;
  startPhoneAudioStreaming: () => Promise<void>;
  stopPhoneAudioStreaming: () => Promise<void>;
  togglePhoneAudio: () => Promise<void>;
}

/**
 * Hook to manage audio streaming from both OMI devices and phone microphone.
 * Handles WebSocket connection setup, JWT authentication, audio data routing,
 * and offline buffering when WebSocket is disconnected.
 */
export const useAudioManager = ({
  webSocketUrl,
  userId,
  jwtToken,
  isAuthenticated,
  omiConnection,
  connectedDeviceId,
  audioStreamer,
  phoneAudioRecorder,
  startAudioListener,
  stopAudioListener,
  offlineMode,
  connectionHandlers,
}: UseAudioManagerParams): UseAudioManagerReturn => {
  const [isPhoneAudioMode, setIsPhoneAudioMode] = useState<boolean>(false);
  const [isOfflineBuffering, setIsOfflineBuffering] = useState<boolean>(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);

  // Track previous WebSocket state to detect transitions
  const previousWsReadyStateRef = useRef<number | undefined>(undefined);
  const sessionIdRef = useRef<string | null>(null);
  const conversationIdRef = useRef<string | null>(null);

  // Generate session ID for offline tracking
  const generateSessionId = useCallback((): string => {
    return `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }, []);

  /**
   * Builds WebSocket URL with authentication parameters and optional endpoint
   */
  const buildWebSocketUrl = useCallback((
    baseUrl: string,
    options?: { deviceName?: string; endpoint?: string }
  ): string => {
    let finalUrl = baseUrl.trim();

    // Convert HTTP/HTTPS to WS/WSS protocol
    if (!finalUrl.startsWith('ws')) {
      finalUrl = finalUrl.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
    }

    // Add endpoint if specified and not already present
    if (options?.endpoint && !finalUrl.includes(options.endpoint)) {
      finalUrl = finalUrl.replace(/\/$/, '') + options.endpoint;
    }

    // Advanced backend requires authentication
    if (jwtToken && isAuthenticated) {
      const params = new URLSearchParams();
      params.append('token', jwtToken);

      const device = options?.deviceName || (userId && userId.trim() !== '' ? userId.trim() : 'phone');
      params.append('device_name', device);

      const separator = finalUrl.includes('?') ? '&' : '?';
      finalUrl = `${finalUrl}${separator}${params.toString()}`;

      console.log('[useAudioManager] Advanced backend WebSocket URL constructed with auth');
    } else {
      console.log('[useAudioManager] Simple backend WebSocket URL (no auth)');
    }

    return finalUrl;
  }, [jwtToken, isAuthenticated, userId]);

  /**
   * Handle audio data with offline buffering support
   */
  const handleAudioData = useCallback(async (audioBytes: Uint8Array) => {
    if (audioBytes.length === 0) return;

    const wsReadyState = audioStreamer.getWebSocketReadyState();
    const wasConnected = previousWsReadyStateRef.current === WebSocket.OPEN;
    const isConnected = wsReadyState === WebSocket.OPEN;

    // Detect disconnect transition
    if (wasConnected && !isConnected && offlineMode && !offlineMode.isOffline) {
      console.log('[useAudioManager] WebSocket disconnected, entering offline mode');
      const sessionId = sessionIdRef.current || generateSessionId();
      sessionIdRef.current = sessionId;
      setCurrentSessionId(sessionId);

      offlineMode.enterOfflineMode(sessionId, conversationIdRef.current);
      setIsOfflineBuffering(true);

      connectionHandlers?.onWebSocketDisconnect?.(sessionId, conversationIdRef.current);
    }

    // Detect reconnect transition
    if (!wasConnected && isConnected && offlineMode?.isOffline) {
      console.log('[useAudioManager] WebSocket reconnected, exiting offline mode');
      await offlineMode.exitOfflineMode();
      setIsOfflineBuffering(false);

      connectionHandlers?.onWebSocketReconnect?.();
    }

    previousWsReadyStateRef.current = wsReadyState;

    // Route audio based on connection state
    if (isConnected) {
      // Online: send via WebSocket
      await audioStreamer.sendAudio(audioBytes);
    } else if (offlineMode?.isOffline) {
      // Offline: buffer locally
      await offlineMode.bufferAudioChunk(audioBytes);
    } else {
      // No offline mode configured, drop audio (legacy behavior)
      console.log('[useAudioManager] Dropping audio - WebSocket not connected, no offline mode');
    }
  }, [
    audioStreamer,
    offlineMode,
    connectionHandlers,
    generateSessionId,
  ]);

  /**
   * Start OMI device audio streaming
   */
  const startOmiAudioStreaming = useCallback(async () => {
    if (!webSocketUrl || webSocketUrl.trim() === '') {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }

    if (!omiConnection.isConnected() || !connectedDeviceId) {
      Alert.alert('Device Not Connected', 'Please connect to an OMI device first.');
      return;
    }

    try {
      // Generate session ID for this streaming session
      const sessionId = generateSessionId();
      sessionIdRef.current = sessionId;
      setCurrentSessionId(sessionId);

      const finalWebSocketUrl = buildWebSocketUrl(webSocketUrl);

      // Start custom WebSocket streaming first
      await audioStreamer.startStreaming(finalWebSocketUrl);

      // Initialize previous state
      previousWsReadyStateRef.current = audioStreamer.getWebSocketReadyState();

      // Then start OMI audio listener with offline-aware handler
      await startAudioListener(handleAudioData);

      console.log('[useAudioManager] OMI audio streaming started successfully', { sessionId });
    } catch (error) {
      console.error('[useAudioManager] Error starting OMI audio streaming:', error);
      Alert.alert('Error', 'Could not start audio listening or streaming.');
      // Cleanup on error
      if (audioStreamer.isStreaming) audioStreamer.stopStreaming();
      sessionIdRef.current = null;
      setCurrentSessionId(null);
    }
  }, [
    webSocketUrl,
    omiConnection,
    connectedDeviceId,
    audioStreamer,
    startAudioListener,
    buildWebSocketUrl,
    handleAudioData,
    generateSessionId,
  ]);

  /**
   * Stop OMI device audio streaming
   */
  const stopOmiAudioStreaming = useCallback(async () => {
    console.log('[useAudioManager] Stopping OMI audio streaming');

    // Exit offline mode if active
    if (offlineMode?.isOffline) {
      await offlineMode.exitOfflineMode();
      setIsOfflineBuffering(false);
    }

    await stopAudioListener();
    audioStreamer.stopStreaming();

    // Clear session tracking
    sessionIdRef.current = null;
    conversationIdRef.current = null;
    setCurrentSessionId(null);
    setCurrentConversationId(null);
    previousWsReadyStateRef.current = undefined;
  }, [stopAudioListener, audioStreamer, offlineMode]);

  /**
   * Start phone microphone audio streaming
   */
  const startPhoneAudioStreaming = useCallback(async () => {
    if (!webSocketUrl || webSocketUrl.trim() === '') {
      Alert.alert('WebSocket URL Required', 'Please enter the WebSocket URL for streaming.');
      return;
    }

    try {
      // Build WebSocket URL with /ws_pcm endpoint and authentication
      const finalWebSocketUrl = buildWebSocketUrl(webSocketUrl, {
        deviceName: 'phone-mic',
        endpoint: '/ws_pcm',
      });

      // Start WebSocket streaming first
      await audioStreamer.startStreaming(finalWebSocketUrl);

      // Start phone audio recording
      await phoneAudioRecorder.startRecording(async (pcmBuffer: Uint8Array) => {
        const wsReadyState = audioStreamer.getWebSocketReadyState();
        if (wsReadyState === WebSocket.OPEN && pcmBuffer.length > 0) {
          await audioStreamer.sendAudio(pcmBuffer);
        }
      });

      setIsPhoneAudioMode(true);
      console.log('[useAudioManager] Phone audio streaming started successfully');
    } catch (error) {
      console.error('[useAudioManager] Error starting phone audio streaming:', error);
      Alert.alert('Error', 'Could not start phone audio streaming.');
      // Cleanup on error
      if (audioStreamer.isStreaming) audioStreamer.stopStreaming();
      if (phoneAudioRecorder.isRecording) await phoneAudioRecorder.stopRecording();
      setIsPhoneAudioMode(false);
    }
  }, [
    webSocketUrl,
    audioStreamer,
    phoneAudioRecorder,
    buildWebSocketUrl,
  ]);

  /**
   * Stop phone microphone audio streaming
   */
  const stopPhoneAudioStreaming = useCallback(async () => {
    console.log('[useAudioManager] Stopping phone audio streaming');
    await phoneAudioRecorder.stopRecording();
    audioStreamer.stopStreaming();
    setIsPhoneAudioMode(false);
  }, [phoneAudioRecorder, audioStreamer]);

  /**
   * Toggle phone audio on/off
   */
  const togglePhoneAudio = useCallback(async () => {
    if (isPhoneAudioMode || phoneAudioRecorder.isRecording) {
      await stopPhoneAudioStreaming();
    } else {
      await startPhoneAudioStreaming();
    }
  }, [
    isPhoneAudioMode,
    phoneAudioRecorder.isRecording,
    startPhoneAudioStreaming,
    stopPhoneAudioStreaming,
  ]);

  return {
    isPhoneAudioMode,
    isOfflineBuffering,
    currentSessionId,
    currentConversationId,
    startOmiAudioStreaming,
    stopOmiAudioStreaming,
    startPhoneAudioStreaming,
    stopPhoneAudioStreaming,
    togglePhoneAudio,
  };
};
