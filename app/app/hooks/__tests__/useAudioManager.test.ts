import { renderHook, act, waitFor } from '@testing-library/react-native';
import { Alert } from 'react-native';
import { useAudioManager } from '../useAudioManager';

jest.mock('react-native/Libraries/Alert/Alert', () => ({
  alert: jest.fn(),
}));

describe('useAudioManager', () => {
  let mockOmiConnection: any;
  let mockAudioStreamer: any;
  let mockPhoneAudioRecorder: any;
  let mockStartAudioListener: jest.Mock;
  let mockStopAudioListener: jest.Mock;

  beforeEach(() => {
    jest.clearAllMocks();

    mockOmiConnection = {
      isConnected: jest.fn(() => true),
      connectedDeviceId: 'device-123',
    };

    mockAudioStreamer = {
      isStreaming: false,
      isConnecting: false,
      error: null,
      startStreaming: jest.fn(() => Promise.resolve()),
      stopStreaming: jest.fn(),
      sendAudio: jest.fn(() => Promise.resolve()),
      getWebSocketReadyState: jest.fn(() => WebSocket.OPEN),
    };

    mockPhoneAudioRecorder = {
      isRecording: false,
      isInitializing: false,
      error: null,
      audioLevel: 0,
      startRecording: jest.fn(() => Promise.resolve()),
      stopRecording: jest.fn(() => Promise.resolve()),
    };

    mockStartAudioListener = jest.fn(() => Promise.resolve());
    mockStopAudioListener = jest.fn(() => Promise.resolve());
  });

  const defaultParams = {
    webSocketUrl: 'ws://localhost:8000/ws_pcm',
    userId: 'test-user',
    jwtToken: null,
    isAuthenticated: false,
    omiConnection: mockOmiConnection,
    connectedDeviceId: 'device-123',
    audioStreamer: mockAudioStreamer,
    phoneAudioRecorder: mockPhoneAudioRecorder,
    startAudioListener: mockStartAudioListener,
    stopAudioListener: mockStopAudioListener,
  };

  it('should start OMI audio streaming successfully', async () => {
    const { result } = renderHook(() => useAudioManager(defaultParams));

    await act(async () => {
      await result.current.startOmiAudioStreaming();
    });

    expect(mockAudioStreamer.startStreaming).toHaveBeenCalledWith('ws://localhost:8000/ws_pcm');
    expect(mockStartAudioListener).toHaveBeenCalled();
  });

  it('should build WebSocket URL with JWT authentication', async () => {
    const paramsWithAuth = {
      ...defaultParams,
      jwtToken: 'test-jwt-token',
      isAuthenticated: true,
    };

    const { result } = renderHook(() => useAudioManager(paramsWithAuth));

    await act(async () => {
      await result.current.startOmiAudioStreaming();
    });

    const callArg = mockAudioStreamer.startStreaming.mock.calls[0][0];
    expect(callArg).toContain('token=test-jwt-token');
    expect(callArg).toContain('device_name=');
  });

  it('should alert when WebSocket URL is missing', async () => {
    const paramsNoUrl = {
      ...defaultParams,
      webSocketUrl: '',
    };

    const { result } = renderHook(() => useAudioManager(paramsNoUrl));

    await act(async () => {
      await result.current.startOmiAudioStreaming();
    });

    expect(Alert.alert).toHaveBeenCalledWith(
      'WebSocket URL Required',
      'Please enter the WebSocket URL for streaming.'
    );
    expect(mockAudioStreamer.startStreaming).not.toHaveBeenCalled();
  });

  it('should alert when device is not connected', async () => {
    const paramsNoDevice = {
      ...defaultParams,
      connectedDeviceId: null,
      omiConnection: {
        ...mockOmiConnection,
        isConnected: () => false,
      },
    };

    const { result } = renderHook(() => useAudioManager(paramsNoDevice));

    await act(async () => {
      await result.current.startOmiAudioStreaming();
    });

    expect(Alert.alert).toHaveBeenCalledWith(
      'Device Not Connected',
      'Please connect to an OMI device first.'
    );
  });

  it('should start phone audio streaming successfully', async () => {
    const { result } = renderHook(() => useAudioManager(defaultParams));

    await act(async () => {
      await result.current.startPhoneAudioStreaming();
    });

    expect(mockAudioStreamer.startStreaming).toHaveBeenCalled();
    expect(mockPhoneAudioRecorder.startRecording).toHaveBeenCalled();
    expect(result.current.isPhoneAudioMode).toBe(true);
  });

  it('should add /ws_pcm endpoint for phone audio', async () => {
    const paramsWithoutEndpoint = {
      ...defaultParams,
      webSocketUrl: 'ws://localhost:8000',
    };

    const { result } = renderHook(() => useAudioManager(paramsWithoutEndpoint));

    await act(async () => {
      await result.current.startPhoneAudioStreaming();
    });

    const callArg = mockAudioStreamer.startStreaming.mock.calls[0][0];
    expect(callArg).toContain('/ws_pcm');
  });

  it('should convert HTTP to WebSocket protocol', async () => {
    const paramsWithHttp = {
      ...defaultParams,
      webSocketUrl: 'http://localhost:8000',
    };

    const { result } = renderHook(() => useAudioManager(paramsWithHttp));

    await act(async () => {
      await result.current.startPhoneAudioStreaming();
    });

    const callArg = mockAudioStreamer.startStreaming.mock.calls[0][0];
    expect(callArg).toMatch(/^ws:/);
  });

  it('should stop OMI audio streaming', async () => {
    const { result } = renderHook(() => useAudioManager(defaultParams));

    await act(async () => {
      await result.current.stopOmiAudioStreaming();
    });

    expect(mockStopAudioListener).toHaveBeenCalled();
    expect(mockAudioStreamer.stopStreaming).toHaveBeenCalled();
  });

  it('should stop phone audio streaming and reset mode', async () => {
    const { result } = renderHook(() => useAudioManager(defaultParams));

    // Start first
    await act(async () => {
      await result.current.startPhoneAudioStreaming();
    });

    expect(result.current.isPhoneAudioMode).toBe(true);

    // Then stop
    await act(async () => {
      await result.current.stopPhoneAudioStreaming();
    });

    expect(mockPhoneAudioRecorder.stopRecording).toHaveBeenCalled();
    expect(mockAudioStreamer.stopStreaming).toHaveBeenCalled();
    expect(result.current.isPhoneAudioMode).toBe(false);
  });

  it('should toggle phone audio on and off', async () => {
    const { result } = renderHook(() => useAudioManager(defaultParams));

    // Toggle on
    await act(async () => {
      await result.current.togglePhoneAudio();
    });

    expect(result.current.isPhoneAudioMode).toBe(true);
    expect(mockPhoneAudioRecorder.startRecording).toHaveBeenCalled();

    // Toggle off
    await act(async () => {
      await result.current.togglePhoneAudio();
    });

    expect(result.current.isPhoneAudioMode).toBe(false);
    expect(mockPhoneAudioRecorder.stopRecording).toHaveBeenCalled();
  });

  it('should cleanup on error when starting OMI streaming', async () => {
    mockAudioStreamer.startStreaming.mockRejectedValue(new Error('Connection failed'));
    mockAudioStreamer.isStreaming = true;

    const { result } = renderHook(() =>
      useAudioManager({
        ...defaultParams,
        audioStreamer: mockAudioStreamer,
      })
    );

    await act(async () => {
      await result.current.startOmiAudioStreaming();
    });

    expect(Alert.alert).toHaveBeenCalledWith('Error', expect.any(String));
    expect(mockAudioStreamer.stopStreaming).toHaveBeenCalled();
  });

  it('should include user ID in WebSocket URL when provided', async () => {
    const paramsWithUserId = {
      ...defaultParams,
      userId: 'my-device-123',
      jwtToken: 'test-token',
      isAuthenticated: true,
    };

    const { result } = renderHook(() => useAudioManager(paramsWithUserId));

    await act(async () => {
      await result.current.startOmiAudioStreaming();
    });

    const callArg = mockAudioStreamer.startStreaming.mock.calls[0][0];
    expect(callArg).toContain('device_name=my-device-123');
  });
});
