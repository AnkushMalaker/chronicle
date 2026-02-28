import { useState, useEffect, useCallback, useRef } from 'react';
import { BleManager, State as BluetoothState } from 'react-native-ble-plx';
import { OmiConnection, OmiDevice } from 'friend-lite-react-native';
import { useConnectionLog } from '../contexts/ConnectionLogContext';

interface UseDeviceScanning {
  devices: OmiDevice[];
  scanning: boolean;
  startScan: () => void;
  stopScan: () => void;
  error: string | null;
}

export const useDeviceScanning = (
  bleManager: BleManager | null,
  omiConnection: OmiConnection,
  permissionGranted: boolean,
  isBluetoothOn: boolean,
  requestBluetoothPermission: () => Promise<boolean>
): UseDeviceScanning => {
  const [devices, setDevices] = useState<OmiDevice[]>([]);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stopScanFunctionRef = useRef<(() => void) | null>(null);
  const scanTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const { addEvent } = useConnectionLog();

  const handleStopScan = useCallback(() => {
    console.log('[Scanner] handleStopScan called');
    if (scanTimeoutRef.current) {
      clearTimeout(scanTimeoutRef.current);
      scanTimeoutRef.current = null;
    }
    if (stopScanFunctionRef.current) {
      console.log('[Scanner] Executing stopScanFunctionRef.current()');
      try {
        stopScanFunctionRef.current(); // Execute the stored stop function
      } catch (e: any) {
        console.error('[Scanner] Error calling stop function from omiConnection:', e);
        // Even if stop function errors, we should ensure our state reflects scanning has stopped.
      }
      stopScanFunctionRef.current = null; // Clear it after stopping
    }
    setScanning(false);
    addEvent('scan_stop', 'BLE scan stopped');
    console.log('[Scanner] Scan stopped, scanning state set to false.');
  }, []);

  const startScan = useCallback(async () => {
    console.log('[Scanner] startScan called');
    setError(null);
    setDevices([]);

    if (scanning) {
      console.log('[Scanner] Scan already in progress. Stopping previous scan first.');
      handleStopScan(); // Stop any existing scan before starting a new one
    }

    if (!bleManager) {
      console.error('[Scanner] BleManager not available');
      setError('Bluetooth manager not initialized.');
      return;
    }

    console.log(`[Scanner] Checking conditions: permissionGranted=${permissionGranted}, isBluetoothOn=${isBluetoothOn}`);

    if (!permissionGranted) {
      console.log('[Scanner] Permission not granted. Requesting permission...');
      const granted = await requestBluetoothPermission();
      if (!granted) {
        console.warn('[Scanner] Permission denied after request.');
        setError('Bluetooth permissions are required to scan for devices.');
        return;
      }
      console.log('[Scanner] Permission granted after request.');
    }

    if (!isBluetoothOn) {
      console.warn('[Scanner] Bluetooth is not powered on.');
      setError('Bluetooth is not enabled. Please turn on Bluetooth.');
      return;
    }

    const currentState = await bleManager.state();
    if (currentState !== BluetoothState.PoweredOn) {
        console.warn(`[Scanner] Bluetooth state is ${currentState}, not PoweredOn. Cannot scan.`);
        setError(`Bluetooth is not powered on (state: ${currentState}). Please enable Bluetooth.`);
        return;
    }

    console.log('[Scanner] Starting device scan with omiConnection');
    setScanning(true);
    addEvent('scan_start', 'BLE scan started');

    try {
      stopScanFunctionRef.current = omiConnection.scanForDevices(
        (device: OmiDevice) => { // Single callback for found devices
          console.log(`[Scanner] Device found: ${device.name} (${device.id}), RSSI: ${device.rssi}`);
          setDevices((prevDevices) => {
            const existingDeviceIndex = prevDevices.findIndex((d) => d.id === device.id);
            if (existingDeviceIndex === -1) {
              addEvent('scan_result', `Found: ${device.name || 'Unknown'}`, { deviceId: device.id, deviceName: device.name || undefined, rssi: device.rssi ?? undefined });
              return [...prevDevices, device];
            } else {
              // Update existing device with fresh RSSI
              const updatedDevices = [...prevDevices];
              updatedDevices[existingDeviceIndex] = device;
              return updatedDevices;
            }
          });
        }
        // No error or stop callback here, based on example.tsx usage
      );

      // Set a timeout for the scan, similar to example.tsx
      if (scanTimeoutRef.current) clearTimeout(scanTimeoutRef.current);
      scanTimeoutRef.current = setTimeout(() => {
        console.log('[Scanner] Scan timeout reached (10s). Stopping scan.');
        setError('Scan timed out. No devices found or connection failed within 10 seconds.'); // Optional: set an error or message
        handleStopScan();
      }, 10000); // 10-second timeout

    } catch (scanError: any) {
      console.error('[Scanner] Failed to initiate scan with omiConnection:', scanError);
      setError(`Failed to start scan: ${scanError.message || 'Unknown error'}`);
      handleStopScan(); // Ensure scanning is stopped on initiation error
    }
  }, [
    omiConnection,
    permissionGranted,
    isBluetoothOn,
    requestBluetoothPermission,
    bleManager,
    handleStopScan, // handleStopScan is memoized, safe to include
    scanning // include scanning to allow stopping current scan if startScan is called again
  ]);

  useEffect(() => {
    return () => {
      console.log('[Scanner] Unmounting useDeviceScanning. Ensuring scan is stopped.');
      handleStopScan(); // Use the memoized stop scan handler
    };
  }, [handleStopScan]);

  return { devices, scanning, startScan, stopScan: handleStopScan, error };
};
