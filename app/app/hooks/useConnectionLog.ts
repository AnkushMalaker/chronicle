import { useState, useCallback, useEffect, useRef } from 'react';
import AsyncStorage from '@react-native-async-storage/async-storage';
import {
  ConnectionType,
  ConnectionStatus,
  ConnectionLogEntry,
  ConnectionState,
  generateLogId,
} from '../types/connectionLog';

const STORAGE_KEY = '@chronicle/connection_log';
const MAX_LOG_ENTRIES = 500;

interface UseConnectionLogReturn {
  // Log entries (history)
  entries: ConnectionLogEntry[];

  // Current state of all connections
  connectionState: ConnectionState;

  // Actions
  logEvent: (
    type: ConnectionType,
    status: ConnectionStatus,
    message: string,
    details?: string,
    metadata?: Record<string, unknown>
  ) => void;
  clearLogs: () => void;

  // Loading state
  isLoading: boolean;
}

/**
 * Hook for managing connection status logging across all subsystems.
 * Provides persistent storage and real-time state tracking.
 */
export const useConnectionLog = (): UseConnectionLogReturn => {
  const [entries, setEntries] = useState<ConnectionLogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [connectionState, setConnectionState] = useState<ConnectionState>({
    network: 'unknown',
    server: 'unknown',
    bluetooth: 'unknown',
    websocket: 'unknown',
  });

  // Use ref to track if we've loaded from storage
  const hasLoadedRef = useRef(false);

  // Load entries from storage on mount
  useEffect(() => {
    const loadEntries = async () => {
      if (hasLoadedRef.current) return;
      hasLoadedRef.current = true;

      try {
        const stored = await AsyncStorage.getItem(STORAGE_KEY);
        if (stored) {
          const parsed = JSON.parse(stored) as ConnectionLogEntry[];
          // Convert timestamp strings back to Date objects
          const entriesWithDates = parsed.map(entry => ({
            ...entry,
            timestamp: new Date(entry.timestamp),
          }));
          setEntries(entriesWithDates);

          // Restore connection state from most recent entries
          const restoredState: ConnectionState = {
            network: 'unknown',
            server: 'unknown',
            bluetooth: 'unknown',
            websocket: 'unknown',
          };

          // Find most recent status for each type
          for (const type of ['network', 'server', 'bluetooth', 'websocket'] as ConnectionType[]) {
            const lastEntry = entriesWithDates.find(e => e.type === type);
            if (lastEntry) {
              restoredState[type] = lastEntry.status;
            }
          }
          setConnectionState(restoredState);
        }
      } catch (error) {
        console.error('[useConnectionLog] Failed to load entries:', error);
      } finally {
        setIsLoading(false);
      }
    };

    loadEntries();
  }, []);

  // Save entries to storage (debounced via effect)
  useEffect(() => {
    if (isLoading || entries.length === 0) return;

    const saveEntries = async () => {
      try {
        await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
      } catch (error) {
        console.error('[useConnectionLog] Failed to save entries:', error);
      }
    };

    // Small delay to batch rapid updates
    const timer = setTimeout(saveEntries, 500);
    return () => clearTimeout(timer);
  }, [entries, isLoading]);

  // Log a new connection event
  const logEvent = useCallback(
    (
      type: ConnectionType,
      status: ConnectionStatus,
      message: string,
      details?: string,
      metadata?: Record<string, unknown>
    ) => {
      const entry: ConnectionLogEntry = {
        id: generateLogId(),
        timestamp: new Date(),
        type,
        status,
        message,
        details,
        metadata,
      };

      console.log(`[ConnectionLog] ${type}: ${status} - ${message}`);

      setEntries(prev => {
        // Add new entry at the beginning (most recent first)
        const updated = [entry, ...prev];
        // Trim to max entries
        return updated.slice(0, MAX_LOG_ENTRIES);
      });

      // Update current connection state
      setConnectionState(prev => ({
        ...prev,
        [type]: status,
      }));
    },
    []
  );

  // Clear all logs
  const clearLogs = useCallback(async () => {
    setEntries([]);
    setConnectionState({
      network: 'unknown',
      server: 'unknown',
      bluetooth: 'unknown',
      websocket: 'unknown',
    });

    try {
      await AsyncStorage.removeItem(STORAGE_KEY);
    } catch (error) {
      console.error('[useConnectionLog] Failed to clear storage:', error);
    }
  }, []);

  return {
    entries,
    connectionState,
    logEvent,
    clearLogs,
    isLoading,
  };
};

export default useConnectionLog;
