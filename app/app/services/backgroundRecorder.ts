/**
 * Background Recorder Service - Android Foreground Service for offline recording
 *
 * Uses Notifee to create a foreground service that:
 * - Keeps the app alive in background
 * - Shows persistent notification when recording offline
 * - Maintains Bluetooth connection to OMI device
 * - Continues buffering audio data
 *
 * Note: iOS has different background audio restrictions
 */

import { Platform } from 'react-native';
import notifee, {
  AndroidCategory,
  AndroidImportance,
  AndroidVisibility,
} from '@notifee/react-native';

// Notification IDs
const NOTIFICATION_ID = 'chronicle-offline-recording';
const CHANNEL_ID = 'chronicle-recording';

// Service state
let isServiceRunning = false;
let onStopCallback: (() => void) | null = null;

/**
 * Create the notification channel (Android only)
 * Must be called before showing notifications
 */
export async function createNotificationChannel(): Promise<void> {
  if (Platform.OS !== 'android') return;

  await notifee.createChannel({
    id: CHANNEL_ID,
    name: 'Offline Recording',
    description: 'Shows when Chronicle is recording audio offline',
    importance: AndroidImportance.LOW, // Low importance = no sound
    visibility: AndroidVisibility.PUBLIC,
  });

  console.log('[BackgroundRecorder] Notification channel created');
}

/**
 * Start the foreground service for background recording
 *
 * @param onStop - Callback when user stops recording from notification
 */
export async function startForegroundService(
  onStop?: () => void
): Promise<void> {
  if (Platform.OS !== 'android') {
    console.log('[BackgroundRecorder] Foreground service not available on iOS');
    return;
  }

  if (isServiceRunning) {
    console.log('[BackgroundRecorder] Service already running');
    return;
  }

  onStopCallback = onStop || null;

  try {
    // Ensure channel exists
    await createNotificationChannel();

    // Start foreground service with notification
    await notifee.displayNotification({
      id: NOTIFICATION_ID,
      title: 'Recording Offline',
      body: 'Chronicle is buffering audio while disconnected',
      android: {
        channelId: CHANNEL_ID,
        category: AndroidCategory.SERVICE,
        importance: AndroidImportance.LOW,
        ongoing: true, // Cannot be dismissed
        pressAction: {
          id: 'default',
          launchActivity: 'default', // Opens app when pressed
        },
        actions: [
          {
            title: 'Stop Recording',
            pressAction: {
              id: 'stop',
            },
          },
        ],
        asForegroundService: true,
        // Small icon - will need to be configured in native code
        smallIcon: 'ic_notification',
        color: '#FF0000', // Red to indicate recording
      },
    });

    isServiceRunning = true;
    console.log('[BackgroundRecorder] Foreground service started');
  } catch (error) {
    console.error('[BackgroundRecorder] Failed to start foreground service:', error);
    throw error;
  }
}

/**
 * Update the notification with current recording status
 *
 * @param durationMs - Total buffered duration in milliseconds
 * @param segmentCount - Number of pending segments
 */
export async function updateNotification(
  durationMs: number,
  segmentCount: number
): Promise<void> {
  if (Platform.OS !== 'android' || !isServiceRunning) return;

  const minutes = Math.floor(durationMs / 60000);
  const seconds = Math.floor((durationMs % 60000) / 1000);
  const durationStr = `${minutes}:${seconds.toString().padStart(2, '0')}`;

  const body = segmentCount > 0
    ? `Buffered: ${durationStr} • ${segmentCount} segment${segmentCount !== 1 ? 's' : ''} pending`
    : `Recording: ${durationStr}`;

  try {
    await notifee.displayNotification({
      id: NOTIFICATION_ID,
      title: 'Recording Offline',
      body,
      android: {
        channelId: CHANNEL_ID,
        category: AndroidCategory.SERVICE,
        importance: AndroidImportance.LOW,
        ongoing: true,
        pressAction: {
          id: 'default',
          launchActivity: 'default',
        },
        actions: [
          {
            title: 'Stop Recording',
            pressAction: {
              id: 'stop',
            },
          },
        ],
        asForegroundService: true,
        smallIcon: 'ic_notification',
        color: '#FF0000',
      },
    });
  } catch (error) {
    console.error('[BackgroundRecorder] Failed to update notification:', error);
  }
}

/**
 * Stop the foreground service
 */
export async function stopForegroundService(): Promise<void> {
  if (Platform.OS !== 'android') return;

  if (!isServiceRunning) {
    console.log('[BackgroundRecorder] Service not running');
    return;
  }

  try {
    await notifee.stopForegroundService();
    await notifee.cancelNotification(NOTIFICATION_ID);

    isServiceRunning = false;
    onStopCallback = null;

    console.log('[BackgroundRecorder] Foreground service stopped');
  } catch (error) {
    console.error('[BackgroundRecorder] Failed to stop foreground service:', error);
  }
}

/**
 * Check if the foreground service is currently running
 */
export function isRunning(): boolean {
  return isServiceRunning;
}

/**
 * Handle notification actions (e.g., stop button press)
 * This should be registered in the app's entry point
 */
export async function handleNotificationEvent(
  type: string,
  detail: { notification?: { id?: string }; pressAction?: { id?: string } }
): Promise<void> {
  if (detail.notification?.id !== NOTIFICATION_ID) return;

  if (type === 'ACTION_PRESS' && detail.pressAction?.id === 'stop') {
    console.log('[BackgroundRecorder] Stop action pressed');

    // Call the stop callback if registered
    if (onStopCallback) {
      onStopCallback();
    }

    // Stop the service
    await stopForegroundService();
  }
}

/**
 * Register the notification event handler
 * Call this in the app's entry point (e.g., App.tsx or index.js)
 */
export function registerNotificationHandler(): () => void {
  if (Platform.OS !== 'android') {
    return () => {};
  }

  // Register foreground event handler
  const unsubscribeForeground = notifee.onForegroundEvent(({ type, detail }) => {
    handleNotificationEvent(type.toString(), detail);
  });

  // Register background event handler
  notifee.onBackgroundEvent(async ({ type, detail }) => {
    await handleNotificationEvent(type.toString(), detail);
  });

  console.log('[BackgroundRecorder] Notification handlers registered');

  return unsubscribeForeground;
}

export default {
  createNotificationChannel,
  startForegroundService,
  updateNotification,
  stopForegroundService,
  isRunning,
  handleNotificationEvent,
  registerNotificationHandler,
};
