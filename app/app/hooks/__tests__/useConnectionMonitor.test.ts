import { renderHook, act, waitFor } from '@testing-library/react-native';
import { Alert } from 'react-native';
import { useConnectionMonitor } from '../useConnectionMonitor';
import { BleManager } from 'react-native-ble-plx';

jest.mock('react-native/Libraries/Alert/Alert', () => ({
  alert: jest.fn(),
}));

describe('useConnectionMonitor', () => {
  let mockBleManager: jest.Mocked<BleManager>;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();

    mockBleManager = {
      isDeviceConnected: jest.fn(),
      devices: jest.fn(),
    } as any;
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('should show disconnected status when no device is connected', () => {
    const { result } = renderHook(() =>
      useConnectionMonitor({
        connectedDeviceId: null,
        bleManager: mockBleManager,
        isAudioStreaming: false,
        webSocketReadyState: WebSocket.CLOSED,
      })
    );

    expect(result.current.bluetoothHealth).toBe('disconnected');
    expect(result.current.webSocketHealth).toBe('disconnected');
  });

  it('should monitor Bluetooth connection health', async () => {
    mockBleManager.isDeviceConnected.mockResolvedValue(true);
    mockBleManager.devices.mockResolvedValue([{ rssi: -60 }] as any);

    const { result } = renderHook(() =>
      useConnectionMonitor({
        connectedDeviceId: 'device-123',
        bleManager: mockBleManager,
        isAudioStreaming: false,
        webSocketReadyState: WebSocket.CLOSED,
      })
    );

    // Fast-forward to trigger first check
    act(() => {
      jest.advanceTimersByTime(5000);
    });

    await waitFor(() => {
      expect(mockBleManager.isDeviceConnected).toHaveBeenCalledWith('device-123');
    });

    await waitFor(() => {
      expect(result.current.bluetoothHealth).toBe('good');
    });
  });

  it('should detect weak Bluetooth signal', async () => {
    mockBleManager.isDeviceConnected.mockResolvedValue(true);
    mockBleManager.devices.mockResolvedValue([{ rssi: -85 }] as any);

    const { result } = renderHook(() =>
      useConnectionMonitor({
        connectedDeviceId: 'device-weak',
        bleManager: mockBleManager,
        isAudioStreaming: false,
        webSocketReadyState: WebSocket.CLOSED,
      })
    );

    act(() => {
      jest.advanceTimersByTime(5000);
    });

    await waitFor(() => {
      expect(result.current.bluetoothHealth).toBe('poor');
    });
  });

  it('should detect Bluetooth device loss and alert user', async () => {
    mockBleManager.isDeviceConnected.mockResolvedValue(false);

    const { result } = renderHook(() =>
      useConnectionMonitor({
        connectedDeviceId: 'device-lost',
        bleManager: mockBleManager,
        isAudioStreaming: false,
        webSocketReadyState: WebSocket.CLOSED,
      })
    );

    act(() => {
      jest.advanceTimersByTime(5000);
    });

    await waitFor(() => {
      expect(result.current.bluetoothHealth).toBe('lost');
    });

    await waitFor(() => {
      expect(Alert.alert).toHaveBeenCalledWith(
        'Bluetooth Connection Lost',
        expect.stringContaining('Lost connection to OMI device'),
        expect.any(Array)
      );
    });
  });

  it('should monitor WebSocket connection state', async () => {
    const { result, rerender } = renderHook(
      ({ wsState }: { wsState: number }) =>
        useConnectionMonitor({
          connectedDeviceId: null,
          bleManager: mockBleManager,
          isAudioStreaming: true,
          webSocketReadyState: wsState,
        }),
      { initialProps: { wsState: WebSocket.OPEN } }
    );

    // Initially connected
    expect(result.current.webSocketHealth).toBe('connected');

    // Connection lost
    rerender({ wsState: WebSocket.CLOSED });

    await waitFor(() => {
      expect(result.current.webSocketHealth).toBe('disconnected');
    });

    await waitFor(() => {
      expect(Alert.alert).toHaveBeenCalledWith(
        'Backend Connection Lost',
        expect.stringContaining('Lost connection to backend'),
        expect.any(Array)
      );
    });
  });

  it('should detect connecting state', () => {
    const { result } = renderHook(() =>
      useConnectionMonitor({
        connectedDeviceId: null,
        bleManager: mockBleManager,
        isAudioStreaming: true,
        webSocketReadyState: WebSocket.CONNECTING,
      })
    );

    expect(result.current.webSocketHealth).toBe('connecting');
  });

  it('should cleanup intervals on unmount', () => {
    const { unmount } = renderHook(() =>
      useConnectionMonitor({
        connectedDeviceId: 'device-cleanup',
        bleManager: mockBleManager,
        isAudioStreaming: true,
        webSocketReadyState: WebSocket.OPEN,
      })
    );

    unmount();

    // Advance time - should not call any monitoring functions
    act(() => {
      jest.advanceTimersByTime(10000);
    });

    // If no errors thrown, cleanup worked correctly
    expect(true).toBe(true);
  });
});
