import { useState, useCallback, useEffect, useRef } from 'react';
import { Alert } from 'react-native';
import { OmiConnection, BleAudioCodec, OmiDevice } from 'friend-lite-react-native';
import { BleError, BleManager, Subscription } from 'react-native-ble-plx';
import { useConnectionLog } from '../contexts/ConnectionLogContext';
import { activateWearableAfterConnect } from '../services/wearableActivation';

interface UseDeviceConnection {
  connectedDevice: OmiDevice | null;
  isConnecting: boolean;
  currentCodec: BleAudioCodec | null;
  batteryLevel: number;
  connectToDevice: (deviceId: string) => Promise<void>;
  disconnectFromDevice: () => Promise<void>;
  getAudioCodec: () => Promise<void>;
  getBatteryLevel: () => Promise<void>;
  getRawBatteryLevel: () => Promise<number>;
  connectedDeviceId: string | null;
}

export const useDeviceConnection = (
  omiConnection: OmiConnection,
  diagnosticBleManager: BleManager,
  onDisconnect?: () => void, // Callback for when disconnection happens, e.g., to stop audio listener
  onConnect?: () => void // Callback for when connection happens
): UseDeviceConnection => {
  const [connectedDevice, setConnectedDevice] = useState<OmiDevice | null>(null);
  const [isConnecting, setIsConnecting] = useState<boolean>(false);
  const [currentCodec, setCurrentCodec] = useState<BleAudioCodec | null>(null);
  const [batteryLevel, setBatteryLevel] = useState<number>(-1);
  const [connectedDeviceId, setConnectedDeviceId] = useState<string | null>(null);
  const { addEvent } = useConnectionLog();

  // Debounce guards
  const lastConnectAttemptRef = useRef<number>(0);
  const disconnectTimerRef = useRef<NodeJS.Timeout | null>(null);
  const disconnectMonitorRef = useRef<Subscription | null>(null);
  const connectStartedAtRef = useRef<number | null>(null);
  const intentionalDisconnectRef = useRef(false);

  const removeDisconnectMonitor = useCallback(() => {
    disconnectMonitorRef.current?.remove();
    disconnectMonitorRef.current = null;
  }, []);

  useEffect(() => removeDisconnectMonitor, [removeDisconnectMonitor]);

  const describeDisconnect = useCallback((error: BleError | null, deviceId: string): string => {
    const connectedForMs = connectStartedAtRef.current == null
      ? null
      : Math.max(0, Date.now() - connectStartedAtRef.current);
    const oneLine = (value: unknown) => String(value).replace(/[\r\n]+/g, ' ').slice(0, 300);
    const fields = [
      `device=${deviceId}`,
      `initiated_by_app=${intentionalDisconnectRef.current}`,
      connectedForMs == null ? null : `connected_for_ms=${connectedForMs}`,
      error ? `error_code=${error.errorCode}` : null,
      error?.iosErrorCode != null ? `ios_error_code=${error.iosErrorCode}` : null,
      error?.androidErrorCode != null ? `android_error_code=${error.androidErrorCode}` : null,
      error?.attErrorCode != null ? `att_error_code=${error.attErrorCode}` : null,
      error?.reason ? `reason="${oneLine(error.reason)}"` : null,
      error?.message ? `message="${oneLine(error.message)}"` : null,
    ].filter(Boolean);
    return fields.join(' ');
  }, []);

  const handleConnectionStateChange = useCallback((id: string, state: string) => {
    console.log(`Device ${id} connection state: ${state}`);
    const isNowConnected = state === 'connected';
    setIsConnecting(false);

    if (isNowConnected) {
        connectStartedAtRef.current = Date.now();
        // Cancel any pending disconnect timer (handles BLE flapping)
        if (disconnectTimerRef.current) {
          clearTimeout(disconnectTimerRef.current);
          disconnectTimerRef.current = null;
        }
        setConnectedDeviceId(id);
        addEvent('connect_success', `Connected to ${id}`, { deviceId: id });
        if (onConnect) onConnect();
    } else {
        // Debounce disconnect by 500ms to handle BLE flapping
        if (disconnectTimerRef.current) clearTimeout(disconnectTimerRef.current);
        disconnectTimerRef.current = setTimeout(() => {
          disconnectTimerRef.current = null;
          setConnectedDeviceId(null);
          setConnectedDevice(null);
          setCurrentCodec(null);
          setBatteryLevel(-1);
          addEvent('disconnect', `Disconnected from ${id}`, { deviceId: id });
          if (onDisconnect) onDisconnect();
        }, 500);
    }
  }, [addEvent, onDisconnect, onConnect]);

  const connectToDevice = useCallback(async (deviceId: string) => {
    // Connect debounce: ignore rapid double-taps within 100ms
    const now = Date.now();
    if (now - lastConnectAttemptRef.current < 100) {
      console.log('[Connection] Debounced rapid connect attempt');
      return;
    }
    lastConnectAttemptRef.current = now;

    if (connectedDeviceId && connectedDeviceId !== deviceId) {
      console.log('Disconnecting from previous device before connecting to new one.');
      await disconnectFromDevice();
    }
    if (connectedDeviceId === deviceId) {
        console.log('Already connected or connecting to this device');
        return;
    }

    setIsConnecting(true);
    setConnectedDevice(null);
    setCurrentCodec(null);
    setBatteryLevel(-1);
    addEvent('connect_start', `Connecting to ${deviceId}`, { deviceId });
    intentionalDisconnectRef.current = false;
    connectStartedAtRef.current = Date.now();
    removeDisconnectMonitor();
    disconnectMonitorRef.current = diagnosticBleManager.onDeviceDisconnected(
      deviceId,
      (error, device) => {
        addEvent(
          'disconnect_reason',
          describeDisconnect(error, device?.id ?? deviceId),
          { deviceId: device?.id ?? deviceId },
        );
      },
    );

    let connectionReady = false;
    let connectionDroppedBeforeReady = false;
    let connectedStateDeviceId: string | null = null;
    let reportedConnected = false;
    const reportConnectionStateWhenReady = (id: string, state: string) => {
      if (state === 'connected') {
        connectedStateDeviceId = id;
        if (!connectionReady || reportedConnected) return;
        reportedConnected = true;
      } else if (!connectionReady) {
        connectionDroppedBeforeReady = true;
      }
      handleConnectionStateChange(id, state);
    };

    try {
      const success = await omiConnection.connect(deviceId, reportConnectionStateWhenReady);
      if (success) {
        const activation = await activateWearableAfterConnect(diagnosticBleManager, deviceId);
        if (activation === 'neo_activated') {
          addEvent('device_active', 'Neo Active mode enabled', { deviceId });
        }
        connectionReady = true;
        if (connectionDroppedBeforeReady) {
          throw new Error('Device disconnected before the Active handshake completed');
        }
        reportConnectionStateWhenReady(connectedStateDeviceId ?? deviceId, 'connected');
        console.log('Successfully initiated connection to device:', deviceId);
      } else {
        setIsConnecting(false);
        connectStartedAtRef.current = null;
        removeDisconnectMonitor();
        addEvent('connect_fail', `Connection failed to ${deviceId}`, { deviceId });
        Alert.alert('Connection Failed', 'Could not connect to the device. Please try again.');
      }
    } catch (error) {
      console.error('Connection error:', error);
      intentionalDisconnectRef.current = true;
      try {
        await omiConnection.disconnect();
      } catch (disconnectError) {
        console.warn('Cleanup after connection error failed:', disconnectError);
      }
      setIsConnecting(false);
      setConnectedDevice(null);
      setConnectedDeviceId(null);
      connectStartedAtRef.current = null;
      removeDisconnectMonitor();
      addEvent('connect_fail', `Connection error: ${error}`, { deviceId });
      Alert.alert('Connection Error', String(error));
    }
  }, [omiConnection, diagnosticBleManager, handleConnectionStateChange, connectedDeviceId, addEvent, describeDisconnect, removeDisconnectMonitor]);

  const disconnectFromDevice = useCallback(async () => {
    console.log('Attempting to disconnect...');
    intentionalDisconnectRef.current = true;
    setIsConnecting(false); // No longer attempting to connect if we are disconnecting
    try {
      if (onDisconnect) {
        await onDisconnect(); // Call pre-disconnect cleanup (e.g., stop audio)
      }
      await omiConnection.disconnect();
      console.log('Successfully disconnected.');
      setConnectedDevice(null);
      setConnectedDeviceId(null);
      setCurrentCodec(null);
      setBatteryLevel(-1);
      // The handleConnectionStateChange should also be triggered by the SDK upon disconnection
    } catch (error) {
      console.error('Disconnect error:', error);
      Alert.alert('Disconnect Error', String(error));
      // Even if disconnect fails, reset state as we intend to be disconnected
      setConnectedDevice(null);
      setConnectedDeviceId(null);
      setCurrentCodec(null);
      setBatteryLevel(-1);
    }
  }, [omiConnection, onDisconnect]);

  const getAudioCodec = useCallback(async () => {
    if (!omiConnection.isConnected() || !connectedDeviceId) {
      Alert.alert('Not Connected', 'Please connect to a device first.');
      return;
    }
    try {
      const codecValue = await omiConnection.getAudioCodec();
      setCurrentCodec(codecValue);
      console.log('Audio codec:', codecValue);
    } catch (error) {
      console.error('Get codec error:', error);
      if (String(error).includes('not connected')) {
        setConnectedDevice(null);
        setConnectedDeviceId(null);
        Alert.alert('Connection Lost', 'The device appears to be disconnected. Please reconnect.');
      } else {
        Alert.alert('Error', `Failed to get audio codec: ${error}`);
      }
    }
  }, [omiConnection, connectedDeviceId]);

  const getBatteryLevel = useCallback(async () => {
    if (!omiConnection.isConnected() || !connectedDeviceId) {
      Alert.alert('Not Connected', 'Please connect to a device first.');
      return;
    }
    try {
      const level = await omiConnection.getBatteryLevel();
      setBatteryLevel(level);
      console.log('Battery level:', level);
    } catch (error) {
      console.error('Get battery level error:', error);
      if (String(error).includes('not connected')) {
        setConnectedDevice(null);
        setConnectedDeviceId(null);
        Alert.alert('Connection Lost', 'The device appears to be disconnected. Please reconnect.');
      } else {
        Alert.alert('Error', `Failed to get battery level: ${error}`);
      }
    }
  }, [omiConnection, connectedDeviceId]);

  const getRawBatteryLevel = useCallback(async (): Promise<number> => {
    const level = await omiConnection.getBatteryLevel();
    setBatteryLevel(level);
    return level;
  }, [omiConnection]);

  return {
    connectedDevice,
    isConnecting,
    currentCodec,
    batteryLevel,
    connectToDevice,
    disconnectFromDevice,
    getAudioCodec,
    getBatteryLevel,
    getRawBatteryLevel,
    connectedDeviceId
  };
};
