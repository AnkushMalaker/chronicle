// useAudioStreamer.ts
import { useState, useRef, useCallback, useEffect } from 'react';
import { PermissionsAndroid, Platform } from 'react-native';
import notifee, { AndroidImportance } from '@notifee/react-native';
import NetInfo from '@react-native-community/netinfo';

interface UseAudioStreamer {
  isStreaming: boolean;
  isConnecting: boolean;
  error: string | null;
  startStreaming: (url: string) => Promise<void>;
  getWebSocketReadyState: () => number | undefined;
  stopStreaming: () => void;
  sendAudio: (audioBytes: Uint8Array) => void;
}

// Wyoming Protocol Types
interface WyomingEvent {
  type: string;
  data?: any;
  version?: string;
  payload_length?: number | null;
}

// Audio format constants (matching OMI device format)
const AUDIO_FORMAT = {
  rate: 16000,
  width: 2,
  channels: 1,
  mode: 'streaming',
};

/** -------------------- Foreground Service helpers (NEW) -------------------- */

const FGS_CHANNEL_ID = 'ws_channel';
const FGS_NOTIFICATION_ID = 'ws_foreground';

// Notifee requires registering the foreground service task once.
let _fgsRegistered = false;
function ensureFgsRegistered() {
  if (_fgsRegistered) return;
  notifee.registerForegroundService(async () => {
    // Keep this task alive as long as any foreground notification is active.
    return new Promise(() => {});
  });
  _fgsRegistered = true;
}

async function ensureNotificationPermission() {
  if (Platform.OS === 'android' && Platform.Version >= 33) {
    await PermissionsAndroid.request(
      PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS
    );
  }
}

async function startForegroundServiceNotification(title: string, body: string) {
  ensureFgsRegistered();
  await ensureNotificationPermission();

  // Create channel if needed
  await notifee.createChannel({
    id: FGS_CHANNEL_ID,
    name: 'Streaming',
    importance: AndroidImportance.LOW,
  });

  // Start (or update) the foreground notification
  await notifee.displayNotification({
    id: FGS_NOTIFICATION_ID,
    title,
    body,
    android: {
      channelId: FGS_CHANNEL_ID,
      asForegroundService: true,
      ongoing: true,
      pressAction: { id: 'default' },
    },
  });
}

async function stopForegroundServiceNotification() {
  try {
    await notifee.stopForegroundService();
  } catch {}
  try {
    await notifee.cancelNotification(FGS_NOTIFICATION_ID);
  } catch {}
}

/** -------------------- Hook -------------------- */

export const useAudioStreamer = (): UseAudioStreamer => {
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  const [isConnecting, setIsConnecting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const websocketRef = useRef<WebSocket | null>(null);
  const manuallyStoppedRef = useRef<boolean>(false);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatRef = useRef<NodeJS.Timeout | null>(null);
  const currentUrlRef = useRef<string>('');

  // backoff: 3s, 6s, 12s, ... capped at 30s; up to 10 attempts before showing an error notification
  const reconnectAttemptsRef = useRef<number>(0);
  const MAX_RECONNECT_ATTEMPTS = 10;
  const BASE_RECONNECT_MS = 3000;
  const MAX_RECONNECT_MS = 30000;
  const HEARTBEAT_MS = 25000;

  // Guard state updates after unmount
  const mountedRef = useRef<boolean>(true);
  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const setStateSafe = useCallback(<T,>(setter: (v: T) => void, val: T) => {
    if (mountedRef.current) setter(val);
  }, []);

  // Helper: background-safe, optional notification for errors/info (NEW)
  const notifyInfo = useCallback(async (title: string, body: string) => {
    try {
      await notifee.displayNotification({
        title,
        body,
        android: { channelId: FGS_CHANNEL_ID },
      });
    } catch {
      // ignore if not available
    }
  }, []);

  // Helper: send Wyoming protocol events (UNCHANGED logic)
  const sendWyomingEvent = useCallback(async (event: WyomingEvent, payload?: Uint8Array) => {
    if (!websocketRef.current || websocketRef.current.readyState !== WebSocket.OPEN) {
      console.log('[AudioStreamer] WebSocket not ready for Wyoming event');
      return;
    }
    try {
      event.version = '1.0.0';
      event.payload_length = payload ? payload.length : null;

      const jsonHeader = JSON.stringify(event) + '\n';
      websocketRef.current.send(jsonHeader);
      if (payload?.length) websocketRef.current.send(payload);
    } catch (e) {
      const errorMessage = (e as any).message || 'Error sending Wyoming event.';
      console.error('[AudioStreamer] Error sending Wyoming event:', errorMessage);
      setStateSafe(setError, errorMessage);
    }
  }, [setStateSafe]);

  // Stop (CHANGED): use explicit close code & reason; clear heartbeat; stop FGS
  const stopStreaming = useCallback(async () => {
    manuallyStoppedRef.current = true;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }

    if (websocketRef.current) {
      try {
        // Send audio-stop best-effort
        if (websocketRef.current.readyState === WebSocket.OPEN) {
          const audioStopEvent: WyomingEvent = { type: 'audio-stop', data: { timestamp: Date.now() } };
          await sendWyomingEvent(audioStopEvent);
        }
      } catch {}
      try {
        websocketRef.current.close(1000, 'manual-stop'); // <— explicit manual reason
      } catch {}
      websocketRef.current = null;
    }

    setStateSafe(setIsStreaming, false);
    setStateSafe(setIsConnecting, false);
    await stopForegroundServiceNotification();
  }, [sendWyomingEvent, setStateSafe]);

  // Reconnect (CHANGED): exponential backoff + no Alerts + optional notification on max attempts
  const attemptReconnect = useCallback(() => {
    if (manuallyStoppedRef.current || !currentUrlRef.current) {
      console.log('[AudioStreamer] Not reconnecting: manually stopped or missing URL');
      return;
    }
    if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
      console.log('[AudioStreamer] Reconnect attempts exhausted');
      notifyInfo('Connection lost', 'Failed to reconnect after multiple attempts.');
      manuallyStoppedRef.current = true;
      setStateSafe(setIsStreaming, false);
      setStateSafe(setIsConnecting, false);
      return;
    }

    const attempt = reconnectAttemptsRef.current + 1;
    const delay = Math.min(MAX_RECONNECT_MS, BASE_RECONNECT_MS * Math.pow(2, reconnectAttemptsRef.current));
    reconnectAttemptsRef.current = attempt;

    console.log(`[AudioStreamer] Reconnect attempt ${attempt}/${MAX_RECONNECT_ATTEMPTS} in ${delay}ms`);

    if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
    setStateSafe(setIsConnecting, true);

    reconnectTimeoutRef.current = setTimeout(() => {
      if (!manuallyStoppedRef.current) {
        startStreaming(currentUrlRef.current)
          .catch(err => {
            console.error('[AudioStreamer] Reconnection failed:', err?.message || err);
            attemptReconnect();
          });
      }
    }, delay);
  }, [notifyInfo, setStateSafe]);

  // Start (CHANGED): start/refresh FGS before connecting; remove Alerts; set heartbeat
  const startStreaming = useCallback(async (url: string): Promise<void> => {
    const trimmed = (url || '').trim();
    if (!trimmed) {
      const errorMsg = 'WebSocket URL is required.';
      setStateSafe(setError, errorMsg);
      return Promise.reject(new Error(errorMsg));
    }

    currentUrlRef.current = trimmed;
    manuallyStoppedRef.current = false;

    // Network gate
    const netState = await NetInfo.fetch();
    if (!netState.isConnected || !netState.isInternetReachable) {
      const errorMsg = 'No internet connection.';
      setStateSafe(setError, errorMsg);
      return Promise.reject(new Error(errorMsg));
    }

    // Ensure Foreground Service is up so the JS VM isn’t killed when backgrounded
    await startForegroundServiceNotification('Chronicle - Streaming', 'Keeping WebSocket connection alive');

    console.log(`[AudioStreamer] Initializing WebSocket: ${trimmed}`);
    if (websocketRef.current) await stopStreaming(); // close any existing

    setStateSafe(setIsConnecting, true);
    setStateSafe(setError, null);

    return new Promise<void>((resolve, reject) => {
      try {
        const ws = new WebSocket(trimmed);

        ws.onopen = async () => {
          console.log('[AudioStreamer] WebSocket open');
          websocketRef.current = ws;
          reconnectAttemptsRef.current = 0;
          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, true);
          setStateSafe(setError, null);

          // Start heartbeat
          if (heartbeatRef.current) clearInterval(heartbeatRef.current);
          heartbeatRef.current = setInterval(() => {
            try {
              if (websocketRef.current?.readyState === WebSocket.OPEN) {
                websocketRef.current.send(JSON.stringify({ type: 'ping', t: Date.now() }));
              }
            } catch {}
          }, HEARTBEAT_MS);

          try {
            const audioStartEvent: WyomingEvent = { type: 'audio-start', data: AUDIO_FORMAT };
            console.log('[AudioStreamer] Sending audio-start event');
            await sendWyomingEvent(audioStartEvent);
            console.log('[AudioStreamer] ✅ audio-start sent successfully');
          } catch (e) {
            console.error('[AudioStreamer] audio-start failed:', e);
          }

          resolve();
        };

        ws.onmessage = (event) => {
          // Handle server messages if needed
          console.log('[AudioStreamer] Message:', event.data);
        };

        ws.onerror = (e) => {
          const msg = (e as any).message || 'WebSocket connection error.';
          console.error('[AudioStreamer] Error:', msg);
          setStateSafe(setError, msg);
          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, false);
          if (websocketRef.current === ws) websocketRef.current = null;
          reject(new Error(msg));
        };

        ws.onclose = (event) => {
          console.log('[AudioStreamer] Closed. Code:', event.code, 'Reason:', event.reason);
          const isManual = event.code === 1000 && event.reason === 'manual-stop';

          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, false);

          if (websocketRef.current === ws) websocketRef.current = null;

          if (!isManual && !manuallyStoppedRef.current) {
            setStateSafe(setError, 'Connection closed; attempting to reconnect.');
            attemptReconnect();
          }
        };
      } catch (e) {
        const msg = (e as any).message || 'Failed to create WebSocket.';
        console.error('[AudioStreamer] Create WS error:', msg);
        setStateSafe(setError, msg);
        setStateSafe(setIsConnecting, false);
        setStateSafe(setIsStreaming, false);
        reject(new Error(msg));
      }
    });
  }, [attemptReconnect, sendWyomingEvent, setStateSafe, stopStreaming]);

  const sendAudio = useCallback(async (audioBytes: Uint8Array) => {
    if (websocketRef.current && websocketRef.current.readyState === WebSocket.OPEN && audioBytes.length > 0) {
      try {
        console.log(`[AudioStreamer] 📤 Sending audio chunk: ${audioBytes.length} bytes`);
        const audioChunkEvent: WyomingEvent = { type: 'audio-chunk', data: AUDIO_FORMAT };
        await sendWyomingEvent(audioChunkEvent, audioBytes);
      } catch (e) {
        const msg = (e as any).message || 'Error sending audio data.';
        console.error('[AudioStreamer] sendAudio error:', msg);
        setStateSafe(setError, msg);
      }
    } else {
      console.log(
        `[AudioStreamer] NOT sending audio. hasWS=${!!websocketRef.current
        } ready=${websocketRef.current?.readyState === WebSocket.OPEN
        } bytes=${audioBytes.length} actualReady=${websocketRef.current?.readyState}`
      );
    }
  }, [sendWyomingEvent, setStateSafe]);

  const getWebSocketReadyState = useCallback(() => websocketRef.current?.readyState, []);

  /** Connectivity-triggered reconnect (NEW) */
  useEffect(() => {
    const sub = NetInfo.addEventListener(state => {
      const online = !!state.isConnected && !!state.isInternetReachable;
      if (online && !manuallyStoppedRef.current) {
        // If socket isn’t open, try to reconnect with backoff
        const ready = websocketRef.current?.readyState;
        if (ready !== WebSocket.OPEN && currentUrlRef.current) {
          console.log('[AudioStreamer] Network back; scheduling reconnect');
          attemptReconnect();
        }
      }
    });
    return () => sub();
  }, [attemptReconnect]);

  /** Cleanup on unmount (CHANGED): don’t auto-stop streaming; just clear timers */
  useEffect(() => {
    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
        heartbeatRef.current = null;
      }
      // Intentionally NOT calling stopStreaming() to allow background persistence.
      // The owner (screen/app) should call stopStreaming() explicitly when the session ends.
    };
  }, []);

  return {
    isStreaming,
    isConnecting,
    error,
    startStreaming,
    getWebSocketReadyState,
    stopStreaming,
    sendAudio,
  };
};
