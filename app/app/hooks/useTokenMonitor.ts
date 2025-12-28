import { useState, useEffect, useCallback } from 'react';
import { Alert } from 'react-native';

interface UseTokenMonitorParams {
  jwtToken: string | null;
  onTokenExpired: () => void;
}

interface UseTokenMonitorReturn {
  isTokenValid: boolean;
  tokenExpiresAt: Date | null;
  minutesUntilExpiration: number | null;
}

/**
 * Monitors JWT token expiration and alerts user when token expires.
 * Decodes JWT, checks expiration time, and provides warnings before expiry.
 */
export const useTokenMonitor = ({
  jwtToken,
  onTokenExpired,
}: UseTokenMonitorParams): UseTokenMonitorReturn => {
  const [isTokenValid, setIsTokenValid] = useState(true);
  const [tokenExpiresAt, setTokenExpiresAt] = useState<Date | null>(null);
  const [minutesUntilExpiration, setMinutesUntilExpiration] = useState<number | null>(null);

  useEffect(() => {
    if (!jwtToken) {
      setIsTokenValid(false);
      setTokenExpiresAt(null);
      setMinutesUntilExpiration(null);
      return;
    }

    try {
      // Decode JWT to get expiration (JWT format: header.payload.signature)
      const payload = JSON.parse(atob(jwtToken.split('.')[1]));

      if (!payload.exp) {
        console.warn('[useTokenMonitor] JWT token has no expiration');
        setIsTokenValid(true);
        return;
      }

      const expiresAt = new Date(payload.exp * 1000);
      setTokenExpiresAt(expiresAt);

      console.log('[useTokenMonitor] Token expires at:', expiresAt.toLocaleString());

      // Check token validity every minute
      const checkInterval = setInterval(() => {
        const now = new Date();
        const timeRemaining = expiresAt.getTime() - now.getTime();
        const minutesRemaining = Math.floor(timeRemaining / 60000);

        setMinutesUntilExpiration(minutesRemaining);

        // Token expired
        if (now >= expiresAt) {
          console.warn('[useTokenMonitor] Token has expired');
          setIsTokenValid(false);
          clearInterval(checkInterval);

          Alert.alert(
            'Session Expired',
            'Your login session has expired. Please log in again.',
            [
              {
                text: 'OK',
                onPress: () => {
                  console.log('[useTokenMonitor] User acknowledged token expiration');
                  onTokenExpired();
                }
              }
            ]
          );
        }
        // Warn 10 minutes before expiration
        else if (minutesRemaining === 10) {
          Alert.alert(
            'Session Expiring Soon',
            'Your session will expire in 10 minutes. Please save any work.',
            [{ text: 'OK' }]
          );
        }
        // Warn 5 minutes before expiration
        else if (minutesRemaining === 5) {
          Alert.alert(
            'Session Expiring Soon',
            'Your session will expire in 5 minutes. Consider logging in again.',
            [{ text: 'OK' }]
          );
        }
      }, 60000); // Check every minute

      return () => {
        clearInterval(checkInterval);
      };
    } catch (error) {
      console.error('[useTokenMonitor] Error decoding JWT token:', error);
      setIsTokenValid(false);
      setTokenExpiresAt(null);
    }
  }, [jwtToken, onTokenExpired]);

  return {
    isTokenValid,
    tokenExpiresAt,
    minutesUntilExpiration,
  };
};
