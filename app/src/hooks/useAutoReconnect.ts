import { useState, useEffect, useCallback } from 'react';
import { State as BluetoothState } from 'react-native-ble-plx';
import { saveLastConnectedDeviceId, getLastConnectedDeviceId } from '../utils/storage';
import { useConnectionLog } from '../contexts/ConnectionLogContext';

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

  // Auto-reconnect effect
  useEffect(() => {
    if (
      bluetoothState === BluetoothState.PoweredOn &&
      permissionGranted &&
      lastKnownDeviceId &&
      !deviceConnection.connectedDeviceId &&
      !deviceConnection.isConnecting &&
      !scanning &&
      !isAttemptingAutoReconnect &&
      !triedAutoReconnectForCurrentId
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
  ]);

  const handleCancelAutoReconnect = useCallback(async () => {
    if (lastKnownDeviceId) {
      await saveLastConnectedDeviceId(null);
      setLastKnownDeviceId(null);
      setTriedAutoReconnectForCurrentId(true);
    }
    await deviceConnection.disconnectFromDevice();
    setIsAttemptingAutoReconnect(false);
  }, [deviceConnection, lastKnownDeviceId]);

  return {
    lastKnownDeviceId,
    isAttemptingAutoReconnect,
    triedAutoReconnectForCurrentId,
    setLastKnownDeviceId,
    setTriedAutoReconnectForCurrentId,
    handleCancelAutoReconnect,
  };
};
