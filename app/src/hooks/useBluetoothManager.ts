import { useState, useEffect, useCallback } from 'react';
import { Platform, PermissionsAndroid, Permission as ReactNativePermission } from 'react-native';
import { BleManager, State as BluetoothState } from 'react-native-ble-plx';
import { useConnectionLog } from '../contexts/ConnectionLogContext';

// Define a constant for the minimum Android SDK version requiring runtime permissions
// const MIN_ANDROID_SDK_FOR_PERMISSIONS = 31; // Android 12 (S) - Will use Platform.Version

export const useBluetoothManager = () => {
  const [bleManager] = useState(() => new BleManager());
  const [bluetoothState, setBluetoothState] = useState<BluetoothState>(BluetoothState.Unknown);
  const [permissionGranted, setPermissionGranted] = useState(false);
  const [isPermissionsLoading, setIsPermissionsLoading] = useState(true);
  const { addEvent } = useConnectionLog();

  useEffect(() => {
    console.log('[BTManager] Initializing Bluetooth Manager');
    const subscription = bleManager.onStateChange((state) => {
      console.log(`[BTManager] Bluetooth state changed: ${state}`);
      setBluetoothState(state);
      addEvent('bt_state_change', `Bluetooth state: ${state}`);
    }, true);
    return () => {
      console.log('[BTManager] Cleaning up Bluetooth Manager state change subscription');
      subscription.remove();
    };
  }, [bleManager]);

  const checkAndRequestPermissions = useCallback(async () => {
    console.log('[BTManager] checkAndRequestPermissions called');
    setIsPermissionsLoading(true);
    let allPermissionsGranted = false;

    if (Platform.OS === 'android') {
      try {
        const apiLevel = typeof Platform.Version === 'number' ? Platform.Version : parseInt(String(Platform.Version), 10);
        console.log(`[BTManager] Android API Level: ${apiLevel}`);

        let permissionsToRequest: ReactNativePermission[] = [];

        if (apiLevel < 31) { // Android 11 (API 30) and below
          console.log('[BTManager] Requesting ACCESS_FINE_LOCATION for Android < 12');
          permissionsToRequest = [PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION];
        } else { // Android 12 (API 31) and above
          console.log('[BTManager] Requesting BLUETOOTH_SCAN, BLUETOOTH_CONNECT, ACCESS_FINE_LOCATION for Android 12+');
          permissionsToRequest = [
            PermissionsAndroid.PERMISSIONS.BLUETOOTH_SCAN as ReactNativePermission, // Cast because type might be string
            PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT as ReactNativePermission,
            PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION,
          ];
        }
        
        console.log('[BTManager] Android permissions to request:', permissionsToRequest);
        const statuses = await PermissionsAndroid.requestMultiple(permissionsToRequest);
        console.log('[BTManager] Android permission statuses:', statuses);

        allPermissionsGranted = permissionsToRequest.every(
          (permission) => statuses[permission] === PermissionsAndroid.RESULTS.GRANTED
        );

        if (!allPermissionsGranted) {
            const deniedPermissions = permissionsToRequest.filter(p => statuses[p] !== PermissionsAndroid.RESULTS.GRANTED);
            console.warn('[BTManager] Not all Bluetooth permissions granted on Android:', deniedPermissions.join(', '));
        }

      } catch (err) {
        console.error('[BTManager] Error requesting Android permissions:', err);
        allPermissionsGranted = false;
      }
    } else if (Platform.OS === 'ios') {
      // For iOS, if Bluetooth is powered on, it implies permissions are handled (or will be by the OS).
      // react-native-ble-plx relies on this. The app must have Info.plist entries.
      console.log('[BTManager] iOS: Checking if Bluetooth is powered on for permission indication.');
      if (bluetoothState === BluetoothState.PoweredOn) {
        console.log('[BTManager] iOS: Bluetooth is PoweredOn, assuming permissions are okay or will be prompted by OS.');
        allPermissionsGranted = true;
      } else {
        console.log('[BTManager] iOS: Bluetooth is not PoweredOn. Permissions cannot be confirmed as granted.');
        allPermissionsGranted = false;
        // Optionally, inform the user they need to enable Bluetooth in settings.
      }
    }

    setPermissionGranted(allPermissionsGranted);
    if (allPermissionsGranted) {
      console.log('[BTManager] All necessary permissions appear to be granted.');
    } else {
      console.warn('[BTManager] Not all necessary permissions were granted or confirmed.');
    }
    setIsPermissionsLoading(false);
    return allPermissionsGranted;
  }, [bleManager, bluetoothState]);

  // Initial permission check when component mounts or bluetooth state becomes known
  useEffect(() => {
    console.log('[BTManager] Performing initial permission check. Current BT State:', bluetoothState);
    if (bluetoothState !== BluetoothState.Unsupported && bluetoothState !== BluetoothState.Unauthorized) {
        // Only attempt to check/request if BT is not in a definitive error state.
        // `checkAndRequestPermissions` itself will set loading to true then false.
        checkAndRequestPermissions();
    }
  }, [bluetoothState, checkAndRequestPermissions]); // Rerun if BT state changes or on initial mount
  
  return {
    bleManager,
    bluetoothState,
    permissionGranted,
    requestBluetoothPermission: checkAndRequestPermissions,
    isPermissionsLoading,
  };
}; 