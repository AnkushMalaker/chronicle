import { useState, useEffect, useCallback } from 'react';
import { State as BluetoothState } from 'react-native-ble-plx';
import { saveLastConnectedDeviceId, getLastConnectedDeviceId } from '../utils/storage';

interface UseAutoReconnectParams {
  bluetoothState: BluetoothState;
  permissionGranted: boolean;
  connectedDeviceId: string | null;
  isConnecting: boolean;
  scanning: boolean;
  connectToDevice: (deviceId: string) => Promise<void>;
}

interface UseAutoReconnectReturn {
  lastKnownDeviceId: string | null;
  isAttemptingAutoReconnect: boolean;
  triedAutoReconnectForCurrentId: boolean;
  saveConnectedDevice: (deviceId: string | null) => Promise<void>;
  clearLastKnownDevice: () => Promise<void>;
  cancelAutoReconnect: () => Promise<void>;
}

/**
 * Hook to manage automatic reconnection to the last known Bluetooth device.
 *
 * Attempts to reconnect when:
 * - Bluetooth is powered on
 * - Permissions are granted
 * - Not currently connected or connecting
 * - Not currently scanning
 * - A last known device ID exists
 */
export const useAutoReconnect = ({
  bluetoothState,
  permissionGranted,
  connectedDeviceId,
  isConnecting,
  scanning,
  connectToDevice,
}: UseAutoReconnectParams): UseAutoReconnectReturn => {
  const [lastKnownDeviceId, setLastKnownDeviceId] = useState<string | null>(null);
  const [isAttemptingAutoReconnect, setIsAttemptingAutoReconnect] = useState(false);
  const [triedAutoReconnectForCurrentId, setTriedAutoReconnectForCurrentId] = useState(false);

  // Load last known device ID on mount
  useEffect(() => {
    const loadLastDevice = async () => {
      const deviceId = await getLastConnectedDeviceId();
      if (deviceId) {
        console.log('[useAutoReconnect] Loaded last known device ID:', deviceId);
        setLastKnownDeviceId(deviceId);
        setTriedAutoReconnectForCurrentId(false);
      } else {
        console.log('[useAutoReconnect] No last known device ID found');
        setLastKnownDeviceId(null);
        setTriedAutoReconnectForCurrentId(true);
      }
    };
    loadLastDevice();
  }, []);

  // Save connected device ID
  const saveConnectedDevice = useCallback(async (deviceId: string | null) => {
    if (deviceId) {
      console.log('[useAutoReconnect] Saving connected device ID:', deviceId);
      await saveLastConnectedDeviceId(deviceId);
      setLastKnownDeviceId(deviceId);
      setTriedAutoReconnectForCurrentId(false);
    }
  }, []);

  // Clear last known device
  const clearLastKnownDevice = useCallback(async () => {
    console.log('[useAutoReconnect] Clearing last known device ID');
    await saveLastConnectedDeviceId(null);
    setLastKnownDeviceId(null);
    setTriedAutoReconnectForCurrentId(true);
  }, []);

  // Cancel auto-reconnect attempt
  const cancelAutoReconnect = useCallback(async () => {
    console.log('[useAutoReconnect] Cancelling auto-reconnection attempt');
    await clearLastKnownDevice();
    setIsAttemptingAutoReconnect(false);
  }, [clearLastKnownDevice]);

  // Attempt auto-reconnection when conditions are met
  useEffect(() => {
    let cancelled = false;

    const shouldAttemptReconnect = (
      bluetoothState === BluetoothState.PoweredOn &&
      permissionGranted &&
      lastKnownDeviceId &&
      !connectedDeviceId &&
      !isConnecting &&
      !scanning &&
      !isAttemptingAutoReconnect &&
      !triedAutoReconnectForCurrentId
    );

    if (!shouldAttemptReconnect) return;

    const attemptAutoConnect = async () => {
      if (cancelled) return;

      console.log(`[useAutoReconnect] Attempting to auto-reconnect to device: ${lastKnownDeviceId}`);

      if (!cancelled) {
        setIsAttemptingAutoReconnect(true);
        setTriedAutoReconnectForCurrentId(true);
      }

      try {
        await connectToDevice(lastKnownDeviceId!);

        if (!cancelled) {
          console.log(`[useAutoReconnect] Auto-reconnect attempt initiated for ${lastKnownDeviceId}`);
        }
      } catch (error) {
        if (!cancelled) {
          console.error(`[useAutoReconnect] Error auto-reconnecting to ${lastKnownDeviceId}:`, error);
          // Clear the problematic device ID
          await clearLastKnownDevice();
        }
      } finally {
        if (!cancelled) {
          setIsAttemptingAutoReconnect(false);
        }
      }
    };

    attemptAutoConnect();

    // Cleanup function to prevent state updates after unmount
    return () => {
      cancelled = true;
    };
  }, [
    bluetoothState,
    permissionGranted,
    lastKnownDeviceId,
    connectedDeviceId,
    isConnecting,
    scanning,
    connectToDevice,
    triedAutoReconnectForCurrentId,
    isAttemptingAutoReconnect,
    clearLastKnownDevice,
  ]);

  return {
    lastKnownDeviceId,
    isAttemptingAutoReconnect,
    triedAutoReconnectForCurrentId,
    saveConnectedDevice,
    clearLastKnownDevice,
    cancelAutoReconnect,
  };
};
