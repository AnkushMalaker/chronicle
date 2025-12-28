import { renderHook, act, waitFor } from '@testing-library/react-native';
import { Alert } from 'react-native';
import { useTokenMonitor } from '../useTokenMonitor';

// Mock Alert
jest.mock('react-native/Libraries/Alert/Alert', () => ({
  alert: jest.fn(),
}));

describe('useTokenMonitor', () => {
  const mockOnTokenExpired = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  const createMockToken = (expiresInMinutes: number): string => {
    const now = Math.floor(Date.now() / 1000);
    const exp = now + (expiresInMinutes * 60);
    const payload = { exp };
    const encodedPayload = btoa(JSON.stringify(payload));
    return `header.${encodedPayload}.signature`;
  };

  it('should decode JWT and set expiration time', () => {
    const token = createMockToken(60); // Expires in 60 minutes

    const { result } = renderHook(() =>
      useTokenMonitor({
        jwtToken: token,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    expect(result.current.isTokenValid).toBe(true);
    expect(result.current.tokenExpiresAt).toBeInstanceOf(Date);
    expect(result.current.minutesUntilExpiration).toBeNull(); // First check happens after 1 minute
  });

  it('should call onTokenExpired when token expires', async () => {
    const token = createMockToken(0); // Already expired

    const { result } = renderHook(() =>
      useTokenMonitor({
        jwtToken: token,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    // Fast-forward 1 minute to trigger check
    act(() => {
      jest.advanceTimersByTime(60000);
    });

    await waitFor(() => {
      expect(Alert.alert).toHaveBeenCalledWith(
        'Session Expired',
        'Your login session has expired. Please log in again.',
        expect.any(Array)
      );
    });

    // Simulate user pressing OK
    const alertCall = (Alert.alert as jest.Mock).mock.calls[0];
    const okButton = alertCall[2][0];
    act(() => {
      okButton.onPress();
    });

    expect(mockOnTokenExpired).toHaveBeenCalled();
    expect(result.current.isTokenValid).toBe(false);
  });

  it('should warn 10 minutes before expiration', async () => {
    const token = createMockToken(10); // Expires in 10 minutes

    renderHook(() =>
      useTokenMonitor({
        jwtToken: token,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    // Fast-forward 1 minute to trigger first check
    act(() => {
      jest.advanceTimersByTime(60000);
    });

    await waitFor(() => {
      expect(Alert.alert).toHaveBeenCalledWith(
        'Session Expiring Soon',
        'Your session will expire in 10 minutes. Please save any work.',
        expect.any(Array)
      );
    });

    expect(mockOnTokenExpired).not.toHaveBeenCalled();
  });

  it('should warn 5 minutes before expiration', async () => {
    const token = createMockToken(5); // Expires in 5 minutes

    renderHook(() =>
      useTokenMonitor({
        jwtToken: token,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    // Fast-forward 1 minute
    act(() => {
      jest.advanceTimersByTime(60000);
    });

    await waitFor(() => {
      expect(Alert.alert).toHaveBeenCalledWith(
        'Session Expiring Soon',
        'Your session will expire in 5 minutes. Consider logging in again.',
        expect.any(Array)
      );
    });
  });

  it('should handle null token gracefully', () => {
    const { result } = renderHook(() =>
      useTokenMonitor({
        jwtToken: null,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    expect(result.current.isTokenValid).toBe(false);
    expect(result.current.tokenExpiresAt).toBe(null);
    expect(result.current.minutesUntilExpiration).toBe(null);
  });

  it('should handle invalid JWT format', () => {
    const invalidToken = 'not-a-valid-jwt';

    const { result } = renderHook(() =>
      useTokenMonitor({
        jwtToken: invalidToken,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    expect(result.current.isTokenValid).toBe(false);
    expect(result.current.tokenExpiresAt).toBe(null);
  });

  it('should cleanup interval on unmount', () => {
    const token = createMockToken(60);

    const { unmount } = renderHook(() =>
      useTokenMonitor({
        jwtToken: token,
        onTokenExpired: mockOnTokenExpired,
      })
    );

    unmount();

    // Fast-forward time - should not trigger alerts
    act(() => {
      jest.advanceTimersByTime(120000);
    });

    expect(Alert.alert).not.toHaveBeenCalled();
  });
});
