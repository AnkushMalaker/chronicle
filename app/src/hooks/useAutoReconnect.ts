import { useState, useEffect, useCallback, useRef } from 'react';
import { AppState } from 'react-native';
import { State as BluetoothState } from 'react-native-ble-plx';
import { saveLastConnectedDeviceId, getLastConnectedDeviceId } from '../utils/storage';
import { useConnectionLog } from '../contexts/ConnectionLogContext';

const BACKOFF_INITIAL = 10000;   // 10s
const BACKOFF_MAX = 300000;      // 5 min
const MIN_HEALTHY_DURATION = 30000; // 30s

interface UseAutoReconnectParams {
  bluetoothState: BluetoothState;
  permissionGranted: boolean;
  deviceConnection: {
    connectedDeviceId: string | null;
    isConnecting: boolean;
    connectToDevice: (deviceId: string) => Promise<void>;
    disconnectFromDevice: () => Promise<void>;
  };
  scanning: boolean;
  /** When false, the device connects once and does NOT auto-reconnect on drop/launch. */
  autoReconnectEnabled?: boolean;
}

export interface AutoReconnectState {
  lastKnownDeviceId: string | null;
  isAttemptingAutoReconnect: boolean;
  triedAutoReconnectForCurrentId: boolean;
  isRetryingConnection: boolean;
  retryBackoffSeconds: number;
  connectionRetryCount: number;
  setLastKnownDeviceId: (id: string | null) => void;
  setTriedAutoReconnectForCurrentId: (tried: boolean) => void;
  handleCancelAutoReconnect: () => Promise<void>;
}

export const useAutoReconnect = ({
  bluetoothState,
  permissionGranted,
  deviceConnection,
  scanning,
  autoReconnectEnabled = true,
}: UseAutoReconnectParams): AutoReconnectState => {
  const [lastKnownDeviceId, setLastKnownDeviceId] = useState<string | null>(null);
  const [isAttemptingAutoReconnect, setIsAttemptingAutoReconnect] = useState(false);
  const [triedAutoReconnectForCurrentId, setTriedAutoReconnectForCurrentId] = useState(false);
  const { addEvent } = useConnectionLog();

  // Retry / backoff state
  const [isRetryingConnection, setIsRetryingConnection] = useState(false);
  const [retryBackoffSeconds, setRetryBackoffSeconds] = useState(0);
  const [connectionRetryCount, setConnectionRetryCount] = useState(0);
  const backoffMsRef = useRef(0);
  const connectionStartTimeRef = useRef<number | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countdownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevConnectedRef = useRef<string | null>(null);
  // Mirror of lastKnownDeviceId for use inside async timers (avoids stale closures
  // and lets a scheduled retry detect that the user cleared/changed the device).
  const lastKnownDeviceIdRef = useRef<string | null>(null);
  // Self-reference so a failed retry can reschedule itself for persistent retry.
  const scheduleRetryRef = useRef<((deviceId: string, isQuickFailure: boolean) => void) | null>(null);
  // User preference mirror for use inside async timers / mount-only listeners.
  const autoReconnectEnabledRef = useRef<boolean>(autoReconnectEnabled);
  useEffect(() => {
    autoReconnectEnabledRef.current = autoReconnectEnabled;
  }, [autoReconnectEnabled]);

  // Mirror of the connected device id for use inside the (mount-only) AppState listener.
  const connectedDeviceIdRef = useRef<string | null>(null);

  useEffect(() => {
    lastKnownDeviceIdRef.current = lastKnownDeviceId;
  }, [lastKnownDeviceId]);

  useEffect(() => {
    connectedDeviceIdRef.current = deviceConnection.connectedDeviceId;
  }, [deviceConnection.connectedDeviceId]);

  const clearRetryTimers = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (countdownTimerRef.current) {
      clearInterval(countdownTimerRef.current);
      countdownTimerRef.current = null;
    }
    setIsRetryingConnection(false);
    setRetryBackoffSeconds(0);
  }, []);

  const resetBackoff = useCallback(() => {
    backoffMsRef.current = 0;
    setConnectionRetryCount(0);
    clearRetryTimers();
  }, [clearRetryTimers]);

  // Schedule a (re)connect attempt with capped backoff. Used by BOTH the
  // disconnect-recovery path and the launch-reconnect path. On failure it
  // reschedules itself, so a single failed attempt never permanently gives up
  // and never forgets the saved device — only explicit user action clears it.
  const scheduleRetry = useCallback(
    (deviceId: string, isQuickFailure: boolean) => {
      if (!deviceId) return;
      // "Connect once" mode: don't auto-reconnect on drop/launch failure.
      if (!autoReconnectEnabledRef.current) {
        clearRetryTimers();
        return;
      }

      // Adjust backoff: a quick failure (or a never-connected launch attempt)
      // grows the delay; a healthy connection that dropped resets it.
      if (isQuickFailure) {
        backoffMsRef.current =
          backoffMsRef.current === 0
            ? BACKOFF_INITIAL
            : Math.min(backoffMsRef.current * 2, BACKOFF_MAX);
      } else {
        backoffMsRef.current = 0;
      }

      const delay = backoffMsRef.current;
      addEvent(
        'reconnect_backoff',
        `Scheduling retry in ${delay / 1000}s (device: ${deviceId})`,
        { deviceId }
      );

      // Replace any in-flight timers before scheduling a new attempt.
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      if (countdownTimerRef.current) {
        clearInterval(countdownTimerRef.current);
        countdownTimerRef.current = null;
      }

      setIsRetryingConnection(true);
      setRetryBackoffSeconds(Math.ceil(delay / 1000));
      setConnectionRetryCount(c => c + 1);

      // Countdown timer for UI.
      const countdownEnd = Date.now() + delay;
      countdownTimerRef.current = setInterval(() => {
        const remaining = Math.max(0, Math.ceil((countdownEnd - Date.now()) / 1000));
        setRetryBackoffSeconds(remaining);
        if (remaining <= 0 && countdownTimerRef.current) {
          clearInterval(countdownTimerRef.current);
          countdownTimerRef.current = null;
        }
      }, 1000);

      retryTimerRef.current = setTimeout(async () => {
        retryTimerRef.current = null;
        if (countdownTimerRef.current) {
          clearInterval(countdownTimerRef.current);
          countdownTimerRef.current = null;
        }

        // Honor cancellation: the user may have cleared/changed the device while
        // we were waiting (handleCancelAutoReconnect / explicit disconnect).
        if (lastKnownDeviceIdRef.current !== deviceId) {
          setIsRetryingConnection(false);
          setRetryBackoffSeconds(0);
          return;
        }

        setIsAttemptingAutoReconnect(true);
        setIsRetryingConnection(false);
        setRetryBackoffSeconds(0);
        addEvent('reconnect_attempt', `Retrying connection to ${deviceId}`, { deviceId });

        try {
          await deviceConnection.connectToDevice(deviceId);
        } catch (error) {
          console.error(`[AutoReconnect] Retry failed for ${deviceId}:`, error);
          // Persistent retry: reschedule (unless the user cleared the device).
          if (lastKnownDeviceIdRef.current === deviceId) {
            scheduleRetryRef.current?.(deviceId, true);
          }
        } finally {
          setIsAttemptingAutoReconnect(false);
        }
      }, delay);
    },
    [addEvent, deviceConnection]
  );

  useEffect(() => {
    scheduleRetryRef.current = scheduleRetry;
  }, [scheduleRetry]);

  // Load last device on mount
  useEffect(() => {
    const load = async () => {
      const deviceId = await getLastConnectedDeviceId();
      if (deviceId) {
        setLastKnownDeviceId(deviceId);
        setTriedAutoReconnectForCurrentId(false);
      } else {
        setLastKnownDeviceId(null);
        setTriedAutoReconnectForCurrentId(true);
      }
    };
    load();
  }, []);

  // Track connection start/end for backoff calculation
  useEffect(() => {
    const currentConnected = deviceConnection.connectedDeviceId;
    const prevConnected = prevConnectedRef.current;
    prevConnectedRef.current = currentConnected;

    // Connection established
    if (currentConnected && !prevConnected) {
      connectionStartTimeRef.current = Date.now();
      clearRetryTimers();
      return;
    }

    // Connection lost (unexpected disconnect) — schedule a recovery attempt.
    // scheduleRetry handles backoff growth and persistent self-rescheduling.
    if (!currentConnected && prevConnected && lastKnownDeviceId) {
      const startTime = connectionStartTimeRef.current;
      connectionStartTimeRef.current = null;
      const duration = startTime ? Date.now() - startTime : 0;
      const isQuickFailure = duration < MIN_HEALTHY_DURATION;
      scheduleRetryRef.current?.(lastKnownDeviceId, isQuickFailure);
    }
  }, [deviceConnection.connectedDeviceId]);

  // Auto-reconnect on app launch (existing behavior)
  useEffect(() => {
    if (
      autoReconnectEnabled &&
      bluetoothState === BluetoothState.PoweredOn &&
      permissionGranted &&
      lastKnownDeviceId &&
      !deviceConnection.connectedDeviceId &&
      !deviceConnection.isConnecting &&
      !scanning &&
      !isAttemptingAutoReconnect &&
      !triedAutoReconnectForCurrentId &&
      !isRetryingConnection
    ) {
      const attemptAutoConnect = async () => {
        setIsAttemptingAutoReconnect(true);
        setTriedAutoReconnectForCurrentId(true);
        addEvent('reconnect_attempt', `Auto-reconnecting to ${lastKnownDeviceId}`, { deviceId: lastKnownDeviceId });
        try {
          await deviceConnection.connectToDevice(lastKnownDeviceId);
        } catch (error) {
          console.error(`[AutoReconnect] Error reconnecting to ${lastKnownDeviceId}:`, error);
          // Persistent retry: do NOT forget the device on a single failure (it may
          // simply be out of range at launch). Schedule a backoff retry instead;
          // only explicit user action (handleCancelAutoReconnect / Disconnect)
          // clears the saved device.
          scheduleRetryRef.current?.(lastKnownDeviceId, true);
        } finally {
          setIsAttemptingAutoReconnect(false);
        }
      };
      attemptAutoConnect();
    }
  }, [
    autoReconnectEnabled,
    bluetoothState, permissionGranted, lastKnownDeviceId,
    deviceConnection.connectedDeviceId, deviceConnection.isConnecting,
    scanning, deviceConnection.connectToDevice,
    triedAutoReconnectForCurrentId, isAttemptingAutoReconnect,
    isRetryingConnection,
  ]);

  const handleCancelAutoReconnect = useCallback(async () => {
    if (lastKnownDeviceId) {
      await saveLastConnectedDeviceId(null);
      setLastKnownDeviceId(null);
      setTriedAutoReconnectForCurrentId(true);
    }
    resetBackoff();
    await deviceConnection.disconnectFromDevice();
    setIsAttemptingAutoReconnect(false);
  }, [deviceConnection, lastKnownDeviceId, resetBackoff]);

  // Reconnect on return to foreground. On iOS the JS runtime suspends in the
  // background; on resume nothing would otherwise re-establish the BLE link.
  // Resetting triedAutoReconnectForCurrentId lets the launch-reconnect effect
  // re-fire for a known-but-disconnected device.
  useEffect(() => {
    const subscription = AppState.addEventListener('change', nextState => {
      if (nextState !== 'active') return;
      if (!autoReconnectEnabledRef.current) return;
      const deviceId = lastKnownDeviceIdRef.current;
      if (deviceId && !connectedDeviceIdRef.current) {
        setTriedAutoReconnectForCurrentId(false);
      }
    });
    return () => subscription.remove();
  }, []);

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      if (countdownTimerRef.current) clearInterval(countdownTimerRef.current);
    };
  }, []);

  return {
    lastKnownDeviceId,
    isAttemptingAutoReconnect,
    triedAutoReconnectForCurrentId,
    isRetryingConnection,
    retryBackoffSeconds,
    connectionRetryCount,
    setLastKnownDeviceId,
    setTriedAutoReconnectForCurrentId,
    handleCancelAutoReconnect,
  };
};
