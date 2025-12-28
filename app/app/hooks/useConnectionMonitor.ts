import { useState, useEffect, useRef } from 'react';
import { Alert } from 'react-native';
import { BleManager } from 'react-native-ble-plx';

interface UseConnectionMonitorParams {
  connectedDeviceId: string | null;
  bleManager: BleManager | null;
  isAudioStreaming: boolean;
  webSocketReadyState?: number;
}

interface UseConnectionMonitorReturn {
  bluetoothHealth: 'good' | 'poor' | 'lost' | 'disconnected';
  webSocketHealth: 'connected' | 'connecting' | 'disconnected' | 'error';
  lastBluetoothCheck: Date | null;
  lastWebSocketCheck: Date | null;
}

/**
 * Monitors connection health for both Bluetooth and WebSocket connections.
 * Provides alerts when connections are lost or degraded.
 */
export const useConnectionMonitor = ({
  connectedDeviceId,
  bleManager,
  isAudioStreaming,
  webSocketReadyState,
}: UseConnectionMonitorParams): UseConnectionMonitorReturn => {
  const [bluetoothHealth, setBluetoothHealth] = useState<'good' | 'poor' | 'lost' | 'disconnected'>('disconnected');
  const [webSocketHealth, setWebSocketHealth] = useState<'connected' | 'connecting' | 'disconnected' | 'error'>('disconnected');
  const [lastBluetoothCheck, setLastBluetoothCheck] = useState<Date | null>(null);
  const [lastWebSocketCheck, setLastWebSocketCheck] = useState<Date | null>(null);

  const bluetoothAlertShownRef = useRef(false);
  const webSocketAlertShownRef = useRef(false);

  // Monitor Bluetooth connection health
  useEffect(() => {
    if (!connectedDeviceId || !bleManager) {
      setBluetoothHealth('disconnected');
      setLastBluetoothCheck(null);
      bluetoothAlertShownRef.current = false;
      return;
    }

    const monitorInterval = setInterval(async () => {
      try {
        // Check if device is still connected
        const isConnected = await bleManager.isDeviceConnected(connectedDeviceId);

        if (!isConnected) {
          console.error('[useConnectionMonitor] Bluetooth device lost');
          setBluetoothHealth('lost');
          setLastBluetoothCheck(new Date());

          if (!bluetoothAlertShownRef.current) {
            bluetoothAlertShownRef.current = true;
            Alert.alert(
              'Bluetooth Connection Lost',
              'Lost connection to OMI device. Please check if the device is nearby and powered on.',
              [
                {
                  text: 'OK',
                  onPress: () => {
                    bluetoothAlertShownRef.current = false;
                  }
                }
              ]
            );
          }
          return;
        }

        // Check signal strength (RSSI)
        const device = await bleManager.devices([connectedDeviceId]);
        if (device && device.length > 0) {
          const rssi = device[0].rssi;

          if (rssi !== null) {
            if (rssi < -80) {
              setBluetoothHealth('poor');
              console.warn('[useConnectionMonitor] Weak Bluetooth signal:', rssi);
            } else {
              setBluetoothHealth('good');
            }
          } else {
            setBluetoothHealth('good');
          }

          setLastBluetoothCheck(new Date());
        }
      } catch (error) {
        console.error('[useConnectionMonitor] Bluetooth monitoring error:', error);
        setBluetoothHealth('lost');
        setLastBluetoothCheck(new Date());
      }
    }, 5000); // Check every 5 seconds

    return () => clearInterval(monitorInterval);
  }, [connectedDeviceId, bleManager]);

  // Monitor WebSocket connection health
  useEffect(() => {
    if (!isAudioStreaming) {
      setWebSocketHealth('disconnected');
      setLastWebSocketCheck(null);
      webSocketAlertShownRef.current = false;
      return;
    }

    // Map WebSocket ready states
    const updateWebSocketHealth = () => {
      const now = new Date();
      setLastWebSocketCheck(now);

      switch (webSocketReadyState) {
        case WebSocket.CONNECTING:
          setWebSocketHealth('connecting');
          break;
        case WebSocket.OPEN:
          setWebSocketHealth('connected');
          webSocketAlertShownRef.current = false;
          break;
        case WebSocket.CLOSING:
        case WebSocket.CLOSED:
          setWebSocketHealth('disconnected');

          if (!webSocketAlertShownRef.current && isAudioStreaming) {
            webSocketAlertShownRef.current = true;
            Alert.alert(
              'Backend Connection Lost',
              'Lost connection to backend server. Audio streaming has stopped.',
              [
                {
                  text: 'OK',
                  onPress: () => {
                    webSocketAlertShownRef.current = false;
                  }
                }
              ]
            );
          }
          break;
        default:
          setWebSocketHealth('error');
      }
    };

    // Check immediately
    updateWebSocketHealth();

    // Then check every 3 seconds
    const monitorInterval = setInterval(updateWebSocketHealth, 3000);

    return () => clearInterval(monitorInterval);
  }, [isAudioStreaming, webSocketReadyState]);

  return {
    bluetoothHealth,
    webSocketHealth,
    lastBluetoothCheck,
    lastWebSocketCheck,
  };
};
