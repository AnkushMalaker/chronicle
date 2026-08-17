// useAudioStreamer.ts
import { useState, useRef, useCallback, useEffect } from 'react';
import { AppState, PermissionsAndroid, Platform } from 'react-native';
import notifee, { AndroidImportance } from '@notifee/react-native';
import NetInfo from '@react-native-community/netinfo';
import { refreshToken } from '../services/auth';
import { durableAudioSpool, SpoolPacket } from '../services/durableAudioSpool';
import { useConnectionLog, ConnectionEventType, ConnectionEvent } from '../contexts/ConnectionLogContext';
import {
  addPlaybackStateListener,
  addRouteChangeListener,
  cancelResponse,
  scheduleResponse,
  stopVoiceSession,
  type NativePlaybackState,
} from '../../modules/chronicle-duplex-audio';
import {
  PhoneDuplexController,
  type PhoneCaptureBinding,
  type PhoneResumeProof,
} from '../protocol/phoneDuplexController';
import type { VoiceCapabilities } from '../protocol/voiceProtocol';

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
  phonePlaybackState: NativePlaybackState['state'] | null;
  startStreaming: (url: string, config?: StreamStartConfig) => Promise<void>;
  getWebSocketReadyState: () => number | undefined;
  stopStreaming: () => Promise<void>;
  sendAudio: (audioBytes: Uint8Array, durable?: boolean) => void;
}

export interface StreamStartConfig {
  phoneVoice?: {
    captureEpoch: number;
    capabilities: VoiceCapabilities;
    restartCapture: () => Promise<NonNullable<StreamStartConfig['phoneVoice']>>;
    stopCapture: () => Promise<void>;
  };
}

// Wyoming Protocol Types
interface WyomingEvent {
  type: string;
  data?: any;
  version?: string;
  payload_length?: number | null;
}

// Audio format constants (matching OMI device format)
const AMBIENT_AUDIO_FORMAT = {
  rate: 16000,
  width: 2,
  channels: 1,
  mode: 'streaming',
  voice_duplex_protocol: 1,
  capture_epoch: 0,
  processing_profile: 'ambient',
  effects: {
    aec: { reporting: 'unreported', requested: null, available: null, enabled: null },
    noise_suppression: { reporting: 'unreported', requested: null, available: null, enabled: null },
  },
  voice_session_id: null,
};

function phoneAudioFormat(
  config: NonNullable<StreamStartConfig['phoneVoice']>,
  voiceSessionId: string | null = null
) {
  const { capabilities, captureEpoch } = config;
  const processingProfile = capabilities.mode === 'duplex_full'
    ? 'duplex_aec'
    : capabilities.mode === 'duplex_isolated'
      ? 'duplex_isolated'
      : 'half_duplex';
  const effect = (value: VoiceCapabilities['aec']) => ({
    reporting: 'reported',
    requested: value.requested,
    available: value.available,
    enabled: value.enabled,
  });
  return {
    ...AMBIENT_AUDIO_FORMAT,
    capture_epoch: captureEpoch,
    processing_profile: processingProfile,
    effects: {
      aec: effect(capabilities.aec),
      noise_suppression: effect(capabilities.noise_suppression),
    },
    voice_session_id: voiceSessionId,
  };
}

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
  const [phonePlaybackState, setPhonePlaybackState] = useState<
    NativePlaybackState['state'] | null
  >(null);

  const websocketRef = useRef<WebSocket | null>(null);
  const manuallyStoppedRef = useRef<boolean>(false);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatRef = useRef<NodeJS.Timeout | null>(null);
  const currentUrlRef = useRef<string>('');
  const outboundChainRef = useRef<Promise<void>>(Promise.resolve());
  const pendingPacketsRef = useRef<Map<string, SpoolPacket>>(new Map());
  const drainingSpoolRef = useRef<boolean>(false);
  const deferredLivePacketsRef = useRef<SpoolPacket[]>([]);
  const activeAudioFormatRef = useRef<Record<string, unknown>>(AMBIENT_AUDIO_FORMAT);
  const streamConfigRef = useRef<StreamStartConfig | undefined>(undefined);
  const duplexControllerRef = useRef<PhoneDuplexController | null>(null);
  const duplexResumeRef = useRef<PhoneResumeProof | null>(null);
  const duplexUnsupportedRef = useRef(false);
  const duplexSubscriptionsRef = useRef<Array<{ remove: () => void }>>([]);
  const protocolHandshakeTimerRef = useRef<NodeJS.Timeout | null>(null);

  // backoff: 3s, 6s, 12s, ... capped at 30s; up to 10 attempts before showing an error notification
  const reconnectAttemptsRef = useRef<number>(0);
  const MAX_RECONNECT_ATTEMPTS = 10;
  const BASE_RECONNECT_MS = 3000;
  const MAX_RECONNECT_MS = 30000;
  const HEARTBEAT_MS = 25000;

  // Track if we received an auth error so onclose doesn't blindly reconnect
  const authFailedRef = useRef<boolean>(false);

  const clearDuplexSocketState = useCallback(async (preserveResume = true) => {
    if (protocolHandshakeTimerRef.current) {
      clearTimeout(protocolHandshakeTimerRef.current);
      protocolHandshakeTimerRef.current = null;
    }
    duplexSubscriptionsRef.current.forEach((subscription) => subscription.remove());
    duplexSubscriptionsRef.current = [];
    const controller = duplexControllerRef.current;
    duplexControllerRef.current = null;
    if (preserveResume && controller?.resumeProof) {
      duplexResumeRef.current = controller.resumeProof;
    } else if (!preserveResume) {
      duplexResumeRef.current = null;
    }
    await controller?.close();
  }, []);

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

  const sendDurablePacket = useCallback((packet: SpoolPacket) => {
    pendingPacketsRef.current.set(`${packet.segmentId}:${packet.sequence}`, packet);
    outboundChainRef.current = outboundChainRef.current.then(async () => {
      if (websocketRef.current?.readyState !== WebSocket.OPEN) return;
      await sendWyomingEvent(
        {
          type: 'audio-chunk',
          data: {
            ...activeAudioFormatRef.current,
            spool_segment_id: packet.segmentId,
            spool_sequence: packet.sequence,
            captured_at_ms: packet.capturedAtMs,
          },
        },
        packet.payload
      );
    });
  }, [sendWyomingEvent]);

  const drainDurableSpool = useCallback(async () => {
    drainingSpoolRef.current = true;
    try {
      const packets = await durableAudioSpool.pendingPackets();
      packets.forEach(sendDurablePacket);
    } finally {
      deferredLivePacketsRef.current.forEach(sendDurablePacket);
      deferredLivePacketsRef.current = [];
      drainingSpoolRef.current = false;
    }
  }, [sendDurablePacket]);

  // Stop (CHANGED): use explicit close code & reason; clear heartbeat; stop FGS
  const stopStreaming = useCallback(async () => {
    manuallyStoppedRef.current = true;
    durableAudioSpool.close();
    await duplexControllerRef.current?.stopNativeSession();
    await clearDuplexSocketState(false);

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
    activeAudioFormatRef.current = AMBIENT_AUDIO_FORMAT;
    streamConfigRef.current = undefined;
    duplexUnsupportedRef.current = false;
    setStateSafe(setPhonePlaybackState, null);
    await stopForegroundServiceNotification();
  }, [clearDuplexSocketState, sendWyomingEvent, setStateSafe]);

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
  const startStreaming = useCallback(async (
    url: string,
    config?: StreamStartConfig
  ): Promise<void> => {
    const trimmed = (url || '').trim();
    if (!trimmed) {
      const errorMsg = 'WebSocket URL is required.';
      setStateSafe(setError, errorMsg);
      return Promise.reject(new Error(errorMsg));
    }

    let requestedConfig = config ?? streamConfigRef.current;
    if (requestedConfig?.phoneVoice && duplexResumeRef.current) {
      const restarted = await requestedConfig.phoneVoice.restartCapture();
      requestedConfig = { phoneVoice: restarted };
    }
    currentUrlRef.current = trimmed;
    streamConfigRef.current = requestedConfig;
    if (requestedConfig?.phoneVoice) {
      setStateSafe(setPhonePlaybackState, null);
    }
    activeAudioFormatRef.current = requestedConfig?.phoneVoice
      ? phoneAudioFormat(
          requestedConfig.phoneVoice,
          duplexResumeRef.current?.previousVoiceSessionId ?? null
        )
      : AMBIENT_AUDIO_FORMAT;
    manuallyStoppedRef.current = false;
    authFailedRef.current = false;

    // Keep BLE capture and the durable spool alive even when there is no network.
    // Starting this after the reachability gate would let Android suspend the JS
    // runtime during exactly the outage we are trying to survive.
    await startForegroundServiceNotification(
      'Chronicle - Recording',
      'Saving audio securely until the backend is reachable'
    );

    // Network gate
    const netState = await NetInfo.fetch();
    if (!netState.isConnected || !netState.isInternetReachable) {
      const errorMsg = 'No internet connection.';
      setStateSafe(setError, errorMsg);
      logEvent('ws_error', 'Connect aborted: no internet connection');
      return Promise.reject(new Error(errorMsg));
    }

    console.log(`[AudioStreamer] Initializing WebSocket: ${trimmed}`);
    if (websocketRef.current) await stopStreaming(); // close any existing
    streamConfigRef.current = requestedConfig;
    activeAudioFormatRef.current = requestedConfig?.phoneVoice
      ? phoneAudioFormat(
          requestedConfig.phoneVoice,
          duplexResumeRef.current?.previousVoiceSessionId ?? null
        )
      : AMBIENT_AUDIO_FORMAT;

    setStateSafe(setIsConnecting, true);
    setStateSafe(setError, null);

    logEvent('ws_connecting', reconnectAttemptsRef.current > 0 ? 'Opening socket (reconnect)' : 'Opening socket');

    return new Promise<void>((resolve, reject) => {
      try {
        const ws = new WebSocket(trimmed);
        ws.binaryType = 'arraybuffer';

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

          const phoneVoice = streamConfigRef.current?.phoneVoice;
          if (phoneVoice) {
            const controller = new PhoneDuplexController({
              capabilities: phoneVoice.capabilities,
              captureEpoch: phoneVoice.captureEpoch,
              native: { scheduleResponse, cancelResponse, stopVoiceSession },
              resumeProof: duplexResumeRef.current,
              restartCapture: phoneVoice.restartCapture,
              replaceAudioSession: async (
                binding: PhoneCaptureBinding,
                voiceSessionId: string
              ) => {
                if (ws.readyState !== WebSocket.OPEN) {
                  throw new Error('route changed after socket closed');
                }
                await sendWyomingEvent({
                  type: 'audio-stop',
                  data: { timestamp: Date.now(), reason: 'profile_transition' },
                });
                const nextPhoneVoice = {
                  ...binding,
                  restartCapture: phoneVoice.restartCapture,
                  stopCapture: phoneVoice.stopCapture,
                };
                streamConfigRef.current = { phoneVoice: nextPhoneVoice };
                activeAudioFormatRef.current = phoneAudioFormat(
                  nextPhoneVoice,
                  voiceSessionId
                );
                await sendWyomingEvent({
                  type: 'audio-start',
                  data: activeAudioFormatRef.current,
                });
              },
              send: async (voiceEvent) => {
                if (ws.readyState !== WebSocket.OPEN) return;
                ws.send(`${JSON.stringify(voiceEvent)}\n`);
              },
            });
            duplexControllerRef.current = controller;
            duplexSubscriptionsRef.current = [
              addPlaybackStateListener((state) => {
                setStateSafe(setPhonePlaybackState, state.state);
                controller.nativePlaybackChanged(state).catch((cause) =>
                  console.error('[AudioStreamer] Playback ACK failed:', cause)
                );
              }),
              addRouteChangeListener((change) => {
                controller.nativeRouteChanged(change).catch((cause) =>
                  console.error('[AudioStreamer] Route update failed:', cause)
                );
              }),
            ];
          }

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
            const audioStartEvent: WyomingEvent = {
              type: 'audio-start',
              data: activeAudioFormatRef.current,
            };
            console.log('[AudioStreamer] Sending audio-start event');
            await sendWyomingEvent(audioStartEvent);
            await drainDurableSpool();
            console.log('[AudioStreamer] ✅ audio-start sent successfully');
            if (duplexControllerRef.current) {
              protocolHandshakeTimerRef.current = setTimeout(() => {
                const controller = duplexControllerRef.current;
                if (!controller || controller.protocolHandshakeComplete) return;
                setStateSafe(setError, 'server_upgrade_required');
                duplexUnsupportedRef.current = true;
                duplexResumeRef.current = null;
                phoneVoice?.stopCapture().catch(() => undefined);
                try { ws.close(1000, 'server-upgrade-required'); } catch {}
              }, 3_000);
            }
          } catch (e) {
            console.error('[AudioStreamer] audio-start failed:', e);
          }

          resolve();
        };

        ws.onmessage = async (event) => {
          // Parse server messages to detect auth errors
          if (typeof event.data !== 'string') {
            try {
              const binary = event.data instanceof ArrayBuffer
                ? event.data
                : new Uint8Array(event.data);
              await duplexControllerRef.current?.receiveBinary(binary);
            } catch (cause) {
              console.error('[AudioStreamer] Rejected binary response:', cause);
              setStateSafe(setError, 'Invalid duplex response from backend.');
            }
            return;
          }
          try {
            const msg = JSON.parse(event.data);
            // Heartbeat reply — proves the socket is alive end-to-end.
            if (msg.type === 'pong') {
              lastPongRef.current = Date.now();
              return;
            }
            if (msg.type === 'audio-ack') {
              const key = `${msg.spool_segment_id}:${msg.sequence}`;
              const packet = pendingPacketsRef.current.get(key);
              if (packet) {
                pendingPacketsRef.current.delete(key);
                durableAudioSpool.acknowledge(packet).catch((e) =>
                  console.error('[AudioStreamer] Failed to retire acknowledged audio:', e)
                );
              }
              return;
            }
            if (msg.type === 'error' && (msg.error === 'token_expired' || msg.error === 'authentication_failed' || msg.error === 'user_not_found')) {
              console.warn(`[AudioStreamer] Auth error from server: ${msg.error} — ${msg.message}`);
              authFailedRef.current = true;
              setStateSafe(setError, msg.message || 'Session expired. Re-authenticating...');
              return;
            }
            if (msg.type === 'error' && msg.error === 'client_upgrade_required') {
              setStateSafe(setError, 'client_upgrade_required');
              return;
            }
            if (msg.type === 'error' && msg.error === 'resume_rejected') {
              const controller = duplexControllerRef.current;
              const phoneVoice = streamConfigRef.current?.phoneVoice;
              if (!controller || !phoneVoice) {
                throw new Error('resume rejection has no phone capture to replace');
              }
              const replacement = await phoneVoice.restartCapture();
              controller.prepareFreshCapture(replacement);
              duplexResumeRef.current = null;
              await sendWyomingEvent({
                type: 'audio-stop',
                data: { timestamp: Date.now(), reason: 'resume_rejected' },
              });
              streamConfigRef.current = { phoneVoice: replacement };
              activeAudioFormatRef.current = phoneAudioFormat(replacement);
              await sendWyomingEvent({
                type: 'audio-start',
                data: activeAudioFormatRef.current,
              });
              return;
            }
            if (
              msg.type === 'audio-session.started'
              || msg.type === 'voice-session.start'
              || msg.type === 'voice-session.stop'
              || msg.type === 'response.audio'
              || msg.type === 'response.cancel'
            ) {
              const controller = duplexControllerRef.current;
              if (!controller) {
                throw new Error('server sent duplex event to a non-phone transport');
              }
              await controller.receiveControl(msg);
              duplexResumeRef.current = controller.resumeProof;
              if (controller.protocolHandshakeComplete && protocolHandshakeTimerRef.current) {
                clearTimeout(protocolHandshakeTimerRef.current);
                protocolHandshakeTimerRef.current = null;
              }
              return;
            }
          } catch (cause) {
            console.error('[AudioStreamer] Rejected control message:', cause);
          }
          console.log('[AudioStreamer] Message:', event.data.substring(0, 200));
        };

        ws.onerror = (e) => {
          const msg = (e as any).message || 'WebSocket connection error.';
          console.error('[AudioStreamer] Error:', msg);
          logEvent('ws_error', msg);
          setStateSafe(setError, msg);
          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, false);
          if (websocketRef.current === ws) websocketRef.current = null;
          clearDuplexSocketState(true).catch(() => undefined);
          reject(new Error(msg));
        };

        ws.onclose = (event) => {
          console.log('[AudioStreamer] Closed. Code:', event.code, 'Reason:', event.reason);
          const isManual = event.code === 1000 && event.reason === 'manual-stop';
          logEvent('ws_close', `Closed code=${event.code}${event.reason ? ` reason="${event.reason}"` : ''}${isManual ? ' (manual)' : ''}`);

          setStateSafe(setIsConnecting, false);
          setStateSafe(setIsStreaming, false);

          if (websocketRef.current === ws) websocketRef.current = null;

          clearDuplexSocketState(true).catch((cause) =>
            console.error('[AudioStreamer] Failed to fence duplex socket:', cause)
          );

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

          if (!isManual && !manuallyStoppedRef.current && !duplexUnsupportedRef.current) {
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
  }, [attemptReconnect, attemptReLogin, clearDuplexSocketState, drainDurableSpool, notifyInfo, sendWyomingEvent, setStateSafe, stopStreaming, logEvent]);

  const sendAudio = useCallback(async (audioBytes: Uint8Array, durable = true) => {
    if (!audioBytes.length) return;

    if (durable) {
      const packet = durableAudioSpool.append(audioBytes);
      if (drainingSpoolRef.current) {
        deferredLivePacketsRef.current.push(packet);
      } else {
        sendDurablePacket(packet);
      }
      return;
    }
    if (websocketRef.current && websocketRef.current.readyState === WebSocket.OPEN) {
      try {
        console.log(`[AudioStreamer] 📤 Sending audio chunk: ${audioBytes.length} bytes`);
        const audioChunkEvent: WyomingEvent = {
          type: 'audio-chunk',
          data: activeAudioFormatRef.current,
        };
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
  }, [sendDurablePacket, sendWyomingEvent, setStateSafe]);

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
    phonePlaybackState,
    startStreaming,
    getWebSocketReadyState,
    stopStreaming,
    sendAudio,
  };
};
