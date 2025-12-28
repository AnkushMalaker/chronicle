/**
 * useBackgroundRecorder - Integrates background recording with offline mode
 *
 * Automatically starts/stops the Android foreground service when:
 * - Entering offline buffering mode (WebSocket disconnected)
 * - Exiting offline mode (connection restored)
 *
 * Updates the notification with current buffer status periodically
 */

import { useEffect, useRef, useCallback } from 'react';
import { Platform } from 'react-native';
import {
  startForegroundService,
  stopForegroundService,
  updateNotification,
  isRunning,
  createNotificationChannel,
} from '../services/backgroundRecorder';

interface UseBackgroundRecorderParams {
  isOffline: boolean;
  isBuffering: boolean;
  currentBufferDurationMs: number;
  pendingSegmentCount: number;
  onStopRequested?: () => void;
}

interface UseBackgroundRecorderReturn {
  isServiceRunning: boolean;
}

/**
 * Hook to manage Android foreground service for background recording
 */
export const useBackgroundRecorder = ({
  isOffline,
  isBuffering,
  currentBufferDurationMs,
  pendingSegmentCount,
  onStopRequested,
}: UseBackgroundRecorderParams): UseBackgroundRecorderReturn => {
  const isInitializedRef = useRef(false);
  const updateIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Initialize notification channel on mount (Android only)
  useEffect(() => {
    if (Platform.OS !== 'android') return;

    const init = async () => {
      if (isInitializedRef.current) return;

      await createNotificationChannel();
      isInitializedRef.current = true;
    };

    init();
  }, []);

  // Start/stop foreground service based on offline state
  useEffect(() => {
    if (Platform.OS !== 'android') return;

    const manageService = async () => {
      if (isOffline && isBuffering) {
        // Start service when entering offline buffering mode
        if (!isRunning()) {
          console.log('[useBackgroundRecorder] Starting foreground service');
          await startForegroundService(onStopRequested);
        }
      } else {
        // Stop service when exiting offline mode
        if (isRunning()) {
          console.log('[useBackgroundRecorder] Stopping foreground service');
          await stopForegroundService();
        }
      }
    };

    manageService();

    return () => {
      // Cleanup: stop service if component unmounts while offline
      if (isRunning()) {
        stopForegroundService();
      }
    };
  }, [isOffline, isBuffering, onStopRequested]);

  // Update notification periodically when buffering
  useEffect(() => {
    if (Platform.OS !== 'android') return;

    if (isOffline && isBuffering && isRunning()) {
      // Initial update
      updateNotification(currentBufferDurationMs, pendingSegmentCount);

      // Set up periodic updates (every 5 seconds)
      updateIntervalRef.current = setInterval(() => {
        if (isRunning()) {
          updateNotification(currentBufferDurationMs, pendingSegmentCount);
        }
      }, 5000);
    }

    return () => {
      if (updateIntervalRef.current) {
        clearInterval(updateIntervalRef.current);
        updateIntervalRef.current = null;
      }
    };
  }, [isOffline, isBuffering, currentBufferDurationMs, pendingSegmentCount]);

  return {
    isServiceRunning: Platform.OS === 'android' && isRunning(),
  };
};

export default useBackgroundRecorder;
