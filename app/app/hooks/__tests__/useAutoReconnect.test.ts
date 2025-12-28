import { renderHook, act, waitFor } from '@testing-library/react-native';
import { useAutoReconnect } from '../useAutoReconnect';
import { State as BluetoothState } from 'react-native-ble-plx';
import * as storage from '../../utils/storage';

// Mock storage utilities
jest.mock('../../utils/storage');

describe('useAutoReconnect', () => {
  const mockConnectToDevice = jest.fn();
  const mockStorageGetLastDeviceId = storage.getLastConnectedDeviceId as jest.Mock;
  const mockStorageSaveLastDeviceId = storage.saveLastConnectedDeviceId as jest.Mock;

  beforeEach(() => {
    jest.clearAllMocks();
    mockStorageGetLastDeviceId.mockResolvedValue(null);
    mockStorageSaveLastDeviceId.mockResolvedValue(undefined);
  });

  it('should load last known device ID on mount', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-123');

    const { result } = renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(result.current.lastKnownDeviceId).toBe('device-123');
    });

    expect(mockStorageGetLastDeviceId).toHaveBeenCalledTimes(1);
  });

  it('should attempt auto-reconnect when conditions are met', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-456');

    const { result } = renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(mockConnectToDevice).toHaveBeenCalledWith('device-456');
    });

    expect(result.current.isAttemptingAutoReconnect).toBe(false);
    expect(result.current.triedAutoReconnectForCurrentId).toBe(true);
  });

  it('should not attempt auto-reconnect if already connected', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-789');

    renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: 'device-789',
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(mockConnectToDevice).not.toHaveBeenCalled();
    });
  });

  it('should not attempt auto-reconnect if Bluetooth is off', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-xyz');

    renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOff,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(mockConnectToDevice).not.toHaveBeenCalled();
    });
  });

  it('should not attempt auto-reconnect if scanning is in progress', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-scan');

    renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: true,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(mockConnectToDevice).not.toHaveBeenCalled();
    });
  });

  it('should save connected device ID', async () => {
    const { result } = renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await act(async () => {
      await result.current.saveConnectedDevice('new-device-id');
    });

    expect(mockStorageSaveLastDeviceId).toHaveBeenCalledWith('new-device-id');
    expect(result.current.lastKnownDeviceId).toBe('new-device-id');
  });

  it('should clear last known device', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-clear');

    const { result } = renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(result.current.lastKnownDeviceId).toBe('device-clear');
    });

    await act(async () => {
      await result.current.clearLastKnownDevice();
    });

    expect(mockStorageSaveLastDeviceId).toHaveBeenCalledWith(null);
    expect(result.current.lastKnownDeviceId).toBe(null);
  });

  it('should handle connection errors and clear device ID', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('bad-device');
    mockConnectToDevice.mockRejectedValue(new Error('Connection failed'));

    renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    await waitFor(() => {
      expect(mockConnectToDevice).toHaveBeenCalledWith('bad-device');
    });

    await waitFor(() => {
      expect(mockStorageSaveLastDeviceId).toHaveBeenCalledWith(null);
    });
  });

  it('should prevent state updates after unmount', async () => {
    mockStorageGetLastDeviceId.mockResolvedValue('device-unmount');

    // Mock a slow connection attempt
    mockConnectToDevice.mockImplementation(() =>
      new Promise((resolve) => setTimeout(resolve, 1000))
    );

    const { unmount } = renderHook(() =>
      useAutoReconnect({
        bluetoothState: BluetoothState.PoweredOn,
        permissionGranted: true,
        connectedDeviceId: null,
        isConnecting: false,
        scanning: false,
        connectToDevice: mockConnectToDevice,
      })
    );

    // Unmount before connection completes
    unmount();

    // Wait for connection to complete
    await waitFor(() => {
      expect(mockConnectToDevice).toHaveBeenCalled();
    }, { timeout: 2000 });

    // Should not throw "Can't perform a React state update on unmounted component"
    // If test passes without errors, the cancellation logic works
  });
});
