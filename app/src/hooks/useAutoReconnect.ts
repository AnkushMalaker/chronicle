import { useState, useEffect, useCallback, useRef } from 'react';
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

    // Connection lost (unexpected disconnect)
    if (!currentConnected && prevConnected && lastKnownDeviceId) {
      const startTime = connectionStartTimeRef.current;
      connectionStartTimeRef.current = null;
      const duration = startTime ? Date.now() - startTime : 0;

      if (duration >= MIN_HEALTHY_DURATION) {
        // Healthy connection — reset backoff
        backoffMsRef.current = 0;
        setConnectionRetryCount(0);
      } else {
        // Quick failure — increase backoff
        if (backoffMsRef.current === 0) {
          backoffMsRef.current = BACKOFF_INITIAL;
        } else {
          backoffMsRef.current = Math.min(backoffMsRef.current * 2, BACKOFF_MAX);
        }
      }

      const delay = backoffMsRef.current;
      const deviceId = lastKnownDeviceId;
      addEvent('reconnect_backoff', `Scheduling retry in ${delay / 1000}s (device: ${deviceId})`, { deviceId });

      setIsRetryingConnection(true);
      setRetryBackoffSeconds(Math.ceil(delay / 1000));
      setConnectionRetryCount(c => c + 1);

      // Countdown timer for UI
      const countdownEnd = Date.now() + delay;
      countdownTimerRef.current = setInterval(() => {
        const remaining = Math.max(0, Math.ceil((countdownEnd - Date.now()) / 1000));
        setRetryBackoffSeconds(remaining);
        if (remaining <= 0 && countdownTimerRef.current) {
          clearInterval(countdownTimerRef.current);
          countdownTimerRef.current = null;
        }
      }, 1000);

      // Schedule reconnect
      retryTimerRef.current = setTimeout(async () => {
        retryTimerRef.current = null;
        if (countdownTimerRef.current) {
          clearInterval(countdownTimerRef.current);
          countdownTimerRef.current = null;
        }

        if (!deviceId) {
          setIsRetryingConnection(false);
          return;
        }

        setIsAttemptingAutoReconnect(true);
        setIsRetryingConnection(false);
        setRetryBackoffSeconds(0);
        addEvent('reconnect_attempt', `Retrying connection to ${deviceId} (attempt ${connectionRetryCount})`, { deviceId });

        try {
          await deviceConnection.connectToDevice(deviceId);
        } catch (error) {
          console.error(`[AutoReconnect] Retry failed for ${deviceId}:`, error);
          // Let the next disconnect cycle handle further retries
        } finally {
          setIsAttemptingAutoReconnect(false);
        }
      }, delay);
    }
  }, [deviceConnection.connectedDeviceId]);

  // Auto-reconnect on app launch (existing behavior)
  useEffect(() => {
    if (
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
          await saveLastConnectedDeviceId(null);
          setLastKnownDeviceId(null);
        } finally {
          setIsAttemptingAutoReconnect(false);
        }
      };
      attemptAutoConnect();
    }
  }, [
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
