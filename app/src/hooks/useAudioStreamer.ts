// useAudioStreamer.ts
import { useState, useRef, useCallback, useEffect } from 'react';
import { AppState, PermissionsAndroid, Platform } from 'react-native';
import notifee, { AndroidImportance } from '@notifee/react-native';
import NetInfo from '@react-native-community/netinfo';
import { refreshToken } from '../services/auth';
import { playDownlinkAudio } from '../utils/audioPlayback';
import { useConnectionLog, ConnectionEventType, ConnectionEvent } from '../contexts/ConnectionLogContext';

interface UseAudioStreamerOptions {
  /** Called when a new JWT token is obtained via auto-re-login */
  onTokenRefreshed?: (token: string) => void;
  /** When false, the socket connects once and does NOT auto-reconnect on drop. */
  autoReconnectEnabled?: boolean;
}

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

export const useAudioStreamer = (options?: UseAudioStreamerOptions): UseAudioStreamer => {
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

  // Track if we received an auth error so onclose doesn't blindly reconnect
  const authFailedRef = useRef<boolean>(false);

  // User preference: when false, connect once (no auto-reconnect on drop).
  const autoReconnectEnabledRef = useRef<boolean>(options?.autoReconnectEnabled ?? true);
  useEffect(() => {
    autoReconnectEnabledRef.current = options?.autoReconnectEnabled ?? true;
  }, [options?.autoReconnectEnabled]);

  // Zombie-socket detection: timestamp of the last pong from the backend.
  const lastPongRef = useRef<number>(0);
  // Last known network reachability, so we only log actual online/offline
  // transitions (NetInfo fires on many intermediate changes).
  const lastNetOnlineRef = useRef<boolean | null>(null);
  // Notify the user only once when reconnect attempts cross the threshold
  // (we keep retrying indefinitely rather than giving up).
  const exhaustedNotifiedRef = useRef<boolean>(false);

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

  // Diagnostics: surface WebSocket/network lifecycle in the in-app Connection Log
  // (and, via the context, the persistent crash-log file). Held in a ref so the
  // logging helper stays stable and we don't have to thread `addEvent` through
  // every callback's dependency array.
  const { addEvent } = useConnectionLog();
  const addEventRef = useRef(addEvent);
  useEffect(() => { addEventRef.current = addEvent; }, [addEvent]);
  const logEvent = useCallback(
    (type: ConnectionEventType, details?: string, extra?: Partial<ConnectionEvent>) => {
      try { addEventRef.current?.(type, details, extra); } catch {}
    },
    []
  );

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

  // Helper: re-authenticate via the central auth service (silent refresh from
  // stored credentials), then rebuild the WebSocket URL with the fresh token.
  const attemptReLogin = useCallback(async (): Promise<boolean> => {
    const newToken = await refreshToken();
    if (!newToken) {
      console.warn('[AudioStreamer] Re-login failed (no token)');
      return false;
    }

    options?.onTokenRefreshed?.(newToken);

    // Rebuild the current URL with the new token
    currentUrlRef.current = currentUrlRef.current.replace(
      /([?&])token=[^&]*/,
      `$1token=${encodeURIComponent(newToken)}`
    );

    console.log('[AudioStreamer] Re-login successful, token refreshed');
    return true;
  }, [options]);

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

  // Reconnect (persistent): exponential backoff capped at MAX_RECONNECT_MS, and
  // we NEVER permanently give up — giving up would also disable the NetInfo and
  // AppState recovery paths. After MAX_RECONNECT_ATTEMPTS we keep retrying at the
  // capped interval and notify the user once.
  const attemptReconnect = useCallback(() => {
    if (manuallyStoppedRef.current || !currentUrlRef.current) {
      console.log('[AudioStreamer] Not reconnecting: manually stopped or missing URL');
      return;
    }
    // "Connect once" mode: don't auto-reconnect on drop.
    if (!autoReconnectEnabledRef.current) {
      console.log('[AudioStreamer] Auto-reconnect disabled (connect-once mode)');
      setStateSafe(setIsConnecting, false);
      return;
    }

    const attempt = reconnectAttemptsRef.current + 1;
    reconnectAttemptsRef.current = attempt;

    // Cap the exponent so the delay saturates at MAX_RECONNECT_MS rather than
    // overflowing once attempts grow large.
    const exponent = Math.min(attempt - 1, 10);
    const delay = Math.min(MAX_RECONNECT_MS, BASE_RECONNECT_MS * Math.pow(2, exponent));

    if (attempt > MAX_RECONNECT_ATTEMPTS && !exhaustedNotifiedRef.current) {
      exhaustedNotifiedRef.current = true;
      notifyInfo('Connection lost', 'Still trying to reconnect…');
    }

    console.log(`[AudioStreamer] Reconnect attempt ${attempt} in ${delay}ms`);
    logEvent('ws_reconnect', `Attempt ${attempt} in ${Math.round(delay / 1000)}s`);

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
  }, [notifyInfo, setStateSafe, logEvent]);

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
    authFailedRef.current = false;

    // Network gate
    const netState = await NetInfo.fetch();
    if (!netState.isConnected || !netState.isInternetReachable) {
      const errorMsg = 'No internet connection.';
      setStateSafe(setError, errorMsg);
      logEvent('ws_error', 'Connect aborted: no internet connection');
      return Promise.reject(new Error(errorMsg));
    }

    // Ensure Foreground Service is up so the JS VM isn’t killed when backgrounded
    await startForegroundServiceNotification('Chronicle - Streaming', 'Keeping WebSocket connection alive');

    console.log(`[AudioStreamer] Initializing WebSocket: ${trimmed}`);
    if (websocketRef.current) await stopStreaming(); // close any existing

    setStateSafe(setIsConnecting, true);
    setStateSafe(setError, null);

    logEvent('ws_connecting', reconnectAttemptsRef.current > 0 ? 'Opening socket (reconnect)' : 'Opening socket');

    return new Promise<void>((resolve, reject) => {
      try {
        const ws = new WebSocket(trimmed);

        ws.onopen = async () => {
          console.log('[AudioStreamer] WebSocket open');
          logEvent('ws_open', 'WebSocket connected');
          websocketRef.current = ws;
          reconnectAttemptsRef.current = 0;
          exhaustedNotifiedRef.current = false;
          lastPongRef.current = Date.now(); // assume healthy at open
          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, true);
          setStateSafe(setError, null);

          // Start heartbeat. Each tick also checks for a half-open (zombie)
          // socket: if the backend hasn't ponged within 2 heartbeats the TCP
          // connection is dead even though readyState still reads OPEN, so we
          // force-close to trigger onclose → reconnect.
          if (heartbeatRef.current) clearInterval(heartbeatRef.current);
          heartbeatRef.current = setInterval(() => {
            try {
              const sock = websocketRef.current;
              if (sock?.readyState !== WebSocket.OPEN) return;
              if (lastPongRef.current && Date.now() - lastPongRef.current > 2 * HEARTBEAT_MS) {
                console.warn('[AudioStreamer] No pong within 2 heartbeats; closing zombie socket');
                logEvent('ws_error', 'No pong within 2 heartbeats; closing zombie socket');
                try { sock.close(4000, 'zombie-no-pong'); } catch {}
                return;
              }
              sock.send(JSON.stringify({ type: 'ping', t: Date.now() }));
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
          // Parse server messages to detect auth errors
          try {
            const msg = JSON.parse(event.data);
            // Heartbeat reply — proves the socket is alive end-to-end.
            if (msg.type === 'pong') {
              lastPongRef.current = Date.now();
              return;
            }
            if (msg.type === 'error' && (msg.error === 'token_expired' || msg.error === 'authentication_failed' || msg.error === 'user_not_found')) {
              console.warn(`[AudioStreamer] Auth error from server: ${msg.error} — ${msg.message}`);
              authFailedRef.current = true;
              setStateSafe(setError, msg.message || 'Session expired. Re-authenticating...');
              return;
            }
            // Backend→device downlink: play synthesized audio (e.g. TTS reply)
            // out of the phone speaker, just like the HAVPE relay does on-device.
            if (msg.type === 'play-audio' && msg.data) {
              playDownlinkAudio(msg.data).catch((e) =>
                console.warn('[AudioStreamer] Failed to play downlink audio:', e)
              );
              return;
            }
          } catch {
            // Not JSON, that's fine (e.g. binary messages)
          }
          console.log('[AudioStreamer] Message:', typeof event.data === 'string' ? event.data.substring(0, 200) : '(binary)');
        };

        ws.onerror = (e) => {
          const msg = (e as any).message || 'WebSocket connection error.';
          console.error('[AudioStreamer] Error:', msg);
          logEvent('ws_error', msg);
          setStateSafe(setError, msg);
          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, false);
          if (websocketRef.current === ws) websocketRef.current = null;
          reject(new Error(msg));
        };

        ws.onclose = (event) => {
          console.log('[AudioStreamer] Closed. Code:', event.code, 'Reason:', event.reason);
          const isManual = event.code === 1000 && event.reason === 'manual-stop';
          logEvent('ws_close', `Closed code=${event.code}${event.reason ? ` reason="${event.reason}"` : ''}${isManual ? ' (manual)' : ''}`);

          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, false);

          if (websocketRef.current === ws) websocketRef.current = null;

          // Auth failure: try re-login instead of blind reconnect
          if (authFailedRef.current && !manuallyStoppedRef.current && autoReconnectEnabledRef.current) {
            authFailedRef.current = false;
            console.log('[AudioStreamer] Auth failure detected, attempting re-login...');
            logEvent('ws_reauth', 'Auth failed; attempting re-login');
            setStateSafe(setError, 'Session expired. Re-authenticating...');
            attemptReLogin().then(success => {
              if (success && currentUrlRef.current) {
                console.log('[AudioStreamer] Re-login succeeded, reconnecting...');
                logEvent('ws_reauth', 'Re-login succeeded; reconnecting');
                reconnectAttemptsRef.current = 0;
                startStreaming(currentUrlRef.current).catch(err => {
                  console.error('[AudioStreamer] Reconnect after re-login failed:', err);
                  setStateSafe(setError, 'Re-authentication succeeded but reconnection failed.');
                });
              } else {
                console.warn('[AudioStreamer] Re-login failed. Please log in manually.');
                logEvent('ws_reauth', 'Re-login failed; manual login required');
                setStateSafe(setError, 'Session expired. Please log in again in Settings.');
                notifyInfo('Session Expired', 'Please open the app and log in again.');
              }
            });
            return;
          }

          if (!isManual && !manuallyStoppedRef.current) {
            if (autoReconnectEnabledRef.current) {
              setStateSafe(setError, 'Connection closed; attempting to reconnect.');
              attemptReconnect();
            } else {
              setStateSafe(setError, 'Connection closed.');
            }
          }
        };
      } catch (e) {
        const msg = (e as any).message || 'Failed to create WebSocket.';
        console.error('[AudioStreamer] Create WS error:', msg);
        logEvent('ws_error', `Failed to create WebSocket: ${msg}`);
        setStateSafe(setError, msg);
        setStateSafe(setIsConnecting, false);
        setStateSafe(setIsStreaming, false);
        reject(new Error(msg));
      }
    });
  }, [attemptReconnect, attemptReLogin, notifyInfo, sendWyomingEvent, setStateSafe, stopStreaming, logEvent]);

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

      // Log only real transitions so the diagnostics aren't flooded.
      if (lastNetOnlineRef.current !== online) {
        lastNetOnlineRef.current = online;
        logEvent('net_change', online ? `Network online (${state.type})` : `Network offline (${state.type})`);
      }

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
  }, [attemptReconnect, logEvent]);

  /** App-lifecycle reconnect: on return to foreground, if a session is intended
   * but the socket isn't open (the JS VM may have been suspended in background),
   * reconnect. */
  useEffect(() => {
    const subscription = AppState.addEventListener('change', nextState => {
      if (nextState !== 'active') return;
      if (manuallyStoppedRef.current || !currentUrlRef.current) return;
      if (websocketRef.current?.readyState !== WebSocket.OPEN) {
        console.log('[AudioStreamer] Foregrounded; scheduling reconnect');
        logEvent('ws_reconnect', 'App foregrounded; socket not open, reconnecting');
        attemptReconnect();
      }
    });
    return () => subscription.remove();
  }, [attemptReconnect, logEvent]);

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
