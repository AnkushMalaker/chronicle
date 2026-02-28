import { useState, useRef, useCallback, useEffect } from 'react';
import { Alert } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { Subscription, ConnectionPriority } from 'react-native-ble-plx';
import { useConnectionLog } from '../contexts/ConnectionLogContext';

interface UseAudioListener {
  isListeningAudio: boolean;
  audioPacketsReceived: number;
  startAudioListener: (onAudioData: (bytes: Uint8Array) => void) => Promise<void>;
  stopAudioListener: () => Promise<void>;
  isRetrying: boolean;
  retryAttempts: number;
}

export const useAudioListener = (
  omiConnection: OmiConnection,
  isConnected: () => boolean // Function to check current connection status
): UseAudioListener => {
  const [isListeningAudio, setIsListeningAudio] = useState<boolean>(false);
  const [audioPacketsReceived, setAudioPacketsReceived] = useState<number>(0);
  const [isRetrying, setIsRetrying] = useState<boolean>(false);
  const [retryAttempts, setRetryAttempts] = useState<number>(0);
  const { addEvent } = useConnectionLog();

  const audioSubscriptionRef = useRef<Subscription | null>(null);
  const uiUpdateIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const localPacketCounterRef = useRef<number>(0);
  const retryTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const shouldRetryRef = useRef<boolean>(false);
  const currentOnAudioDataRef = useRef<((bytes: Uint8Array) => void) | null>(null);

  // Retry configuration
  const MAX_RETRY_ATTEMPTS = 10;
  const INITIAL_RETRY_DELAY = 1000; // 1 second
  const MAX_RETRY_DELAY = 60000; // 60 seconds

  const stopAudioListener = useCallback(async () => {
    console.log('Attempting to stop audio listener...');

    // Stop retry mechanism
    shouldRetryRef.current = false;
    setIsRetrying(false);
    setRetryAttempts(0);
    currentOnAudioDataRef.current = null;

    if (retryTimeoutRef.current) {
      clearTimeout(retryTimeoutRef.current);
      retryTimeoutRef.current = null;
    }

    if (uiUpdateIntervalRef.current) {
      clearInterval(uiUpdateIntervalRef.current);
      uiUpdateIntervalRef.current = null;
    }

    if (audioSubscriptionRef.current) {
      try {
        await omiConnection.stopAudioBytesListener(audioSubscriptionRef.current);
        audioSubscriptionRef.current = null;
        setIsListeningAudio(false);
        localPacketCounterRef.current = 0; // Reset local counter
        // setAudioPacketsReceived(0); // Optionally reset global counter on stop, or keep cumulative
        addEvent('audio_stop', 'Audio listener stopped');
        console.log('Audio listener stopped.');
      } catch (error) {
        console.error('Stop audio listener error:', error);
        Alert.alert('Error', `Failed to stop audio listener: ${error}`);
      }
    } else {
      console.log('Audio listener was not active.');
    }
    setIsListeningAudio(false); // Ensure state is false even if no subscription was found
  }, [omiConnection]);

  // Calculate exponential backoff delay
  const getRetryDelay = useCallback((attemptNumber: number): number => {
    const delay = Math.min(INITIAL_RETRY_DELAY * Math.pow(2, attemptNumber), MAX_RETRY_DELAY);
    // Add jitter to prevent thundering herd
    const jitter = Math.random() * 0.3 * delay;
    return delay + jitter;
  }, []);

  // Internal function to attempt starting audio listener
  const attemptStartAudioListener = useCallback(async (onAudioData: (bytes: Uint8Array) => void): Promise<boolean> => {
    if (!isConnected()) {
      console.log('[AudioListener] Device not connected, cannot start audio listener');
      return false;
    }

    try {
      // Request high connection priority before starting audio listener
      await omiConnection.requestConnectionPriority(ConnectionPriority.High);
      console.log('[AudioListener] Requested high connection priority');
    } catch (error) {
      console.error('[AudioListener] Failed to request high connection priority:', error);
      // Continue anyway, this is not a fatal error
    }

    try {
      const subscription = await omiConnection.startAudioBytesListener((bytes) => {
        localPacketCounterRef.current++;
        if (bytes && bytes.length > 0) {
          onAudioData(new Uint8Array(bytes));
        }
      });

      if (subscription) {
        audioSubscriptionRef.current = subscription;
        setIsListeningAudio(true);
        setIsRetrying(false);
        setRetryAttempts(0);
        addEvent('audio_start', 'Audio listener started');
        console.log('[AudioListener] Audio listener started successfully');
        return true;
      } else {
        console.error('[AudioListener] No subscription returned from startAudioBytesListener');
        return false;
      }
    } catch (error) {
      console.error('[AudioListener] Failed to start audio listener:', error);
      return false;
    }
  }, [omiConnection, isConnected]);

  // Retry mechanism with exponential backoff
  const retryStartAudioListener = useCallback(async () => {
    if (!shouldRetryRef.current || !currentOnAudioDataRef.current) {
      console.log('[AudioListener] Retry cancelled or no callback available');
      return;
    }

    const currentAttempt = retryAttempts;
    if (currentAttempt >= MAX_RETRY_ATTEMPTS) {
      console.log(`[AudioListener] Maximum retry attempts (${MAX_RETRY_ATTEMPTS}) reached`);
      setIsRetrying(false);
      setIsListeningAudio(false);
      Alert.alert(
        'Audio Listener Failed',
        `Failed to start audio listener after ${MAX_RETRY_ATTEMPTS} attempts. Please try again manually.`
      );
      return;
    }

    console.log(`[AudioListener] Retry attempt ${currentAttempt + 1}/${MAX_RETRY_ATTEMPTS}`);
    setRetryAttempts(currentAttempt + 1);
    setIsRetrying(true);

    const success = await attemptStartAudioListener(currentOnAudioDataRef.current);

    if (success) {
      console.log('[AudioListener] Retry successful');
      return;
    }

    // If still should retry, schedule next attempt
    if (shouldRetryRef.current) {
      const delay = getRetryDelay(currentAttempt);
      console.log(`[AudioListener] Scheduling retry in ${Math.round(delay)}ms`);

      retryTimeoutRef.current = setTimeout(() => {
        if (shouldRetryRef.current) {
          retryStartAudioListener();
        }
      }, delay);
    }
  }, [retryAttempts, attemptStartAudioListener, getRetryDelay]);

  const startAudioListener = useCallback(async (onAudioData: (bytes: Uint8Array) => void) => {
    if (!isConnected()) {
      Alert.alert('Not Connected', 'Please connect to a device first to start audio listener.');
      return;
    }

    if (isListeningAudio) {
      console.log('[AudioListener] Audio listener is already active. Stopping first.');
      await stopAudioListener();
    }

    // Store the callback for retry attempts
    currentOnAudioDataRef.current = onAudioData;
    shouldRetryRef.current = true;

    setAudioPacketsReceived(0); // Reset counter on start
    localPacketCounterRef.current = 0;
    setRetryAttempts(0);
    console.log('[AudioListener] Starting audio bytes listener...');

    // Batch UI updates for packet counter
    if (uiUpdateIntervalRef.current) clearInterval(uiUpdateIntervalRef.current);
    uiUpdateIntervalRef.current = setInterval(() => {
      if (localPacketCounterRef.current > 0) {
        setAudioPacketsReceived(prev => prev + localPacketCounterRef.current);
        localPacketCounterRef.current = 0;
      }
    }, 500); // Update UI every 500ms

    // Try to start audio listener
    const success = await attemptStartAudioListener(onAudioData);

    if (!success && shouldRetryRef.current) {
      console.log('[AudioListener] Initial attempt failed, starting retry mechanism');
      setIsRetrying(true);
      // Start retry mechanism
      retryStartAudioListener();
    }
  }, [omiConnection, isConnected, stopAudioListener, attemptStartAudioListener, retryStartAudioListener]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      shouldRetryRef.current = false;
      if (retryTimeoutRef.current) {
        clearTimeout(retryTimeoutRef.current);
      }
      if (uiUpdateIntervalRef.current) {
        clearInterval(uiUpdateIntervalRef.current);
      }
    };
  }, []);

  return {
    isListeningAudio,
    audioPacketsReceived,
    startAudioListener,
    stopAudioListener,
    isRetrying,
    retryAttempts,
  };
};
