/**
 * useOfflineMode - Manages offline audio buffering state
 *
 * Tracks:
 * - Whether app is in offline buffering mode
 * - Pending segments waiting to upload
 * - Storage statistics
 * - Reconnection handling
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import {
  initOfflineStorage,
  getPendingSegments,
  getStorageStats,
  getLastActiveConversationId,
  closeOfflineStorage,
  OfflineStorageStats,
  PendingSegment,
} from '../storage/offlineStorage';
import {
  startBuffer,
  addChunk,
  finalizeBuffer,
  cancelBuffer,
  isBufferActive,
  getBufferStats,
  rotateBuffer,
} from '../storage/audioBuffer';

// Storage warning threshold (500MB)
const STORAGE_WARNING_BYTES = 500 * 1024 * 1024;

export interface OfflineModeState {
  isOffline: boolean;
  isBuffering: boolean;
  pendingSegments: PendingSegment[];
  stats: OfflineStorageStats;
  currentBufferDurationMs: number;
  storageWarning: boolean;
  lastActiveConversationId: string | null;
}

export interface UseOfflineModeReturn extends OfflineModeState {
  // Initialization
  initialize: () => Promise<void>;
  cleanup: () => Promise<void>;

  // Mode control
  enterOfflineMode: (sessionId: string, conversationId: string | null) => void;
  exitOfflineMode: () => Promise<PendingSegment | null>;

  // Audio buffering
  bufferAudioChunk: (chunk: Uint8Array) => Promise<PendingSegment | null>;

  // Sync management
  refreshPendingSegments: () => Promise<void>;
  refreshStats: () => Promise<void>;
}

export const useOfflineMode = (): UseOfflineModeReturn => {
  const [isOffline, setIsOffline] = useState(false);
  const [isBuffering, setIsBuffering] = useState(false);
  const [pendingSegments, setPendingSegments] = useState<PendingSegment[]>([]);
  const [stats, setStats] = useState<OfflineStorageStats>({
    totalSegments: 0,
    pendingSegments: 0,
    totalBytes: 0,
    oldestSegmentAge: null,
  });
  const [currentBufferDurationMs, setCurrentBufferDurationMs] = useState(0);
  const [storageWarning, setStorageWarning] = useState(false);
  const [lastActiveConversationId, setLastActiveConversationId] = useState<string | null>(null);

  const isInitializedRef = useRef(false);
  const currentSessionIdRef = useRef<string | null>(null);
  const currentConversationIdRef = useRef<string | null>(null);
  const bufferUpdateIntervalRef = useRef<NodeJS.Timeout | null>(null);

  /**
   * Initialize offline storage system
   */
  const initialize = useCallback(async () => {
    if (isInitializedRef.current) return;

    try {
      await initOfflineStorage();
      isInitializedRef.current = true;

      // Load initial state
      const [segments, storageStats, lastConversationId] = await Promise.all([
        getPendingSegments(),
        getStorageStats(),
        getLastActiveConversationId(),
      ]);

      setPendingSegments(segments);
      setStats(storageStats);
      setLastActiveConversationId(lastConversationId);
      setStorageWarning(storageStats.totalBytes >= STORAGE_WARNING_BYTES);

      console.log('[useOfflineMode] Initialized', {
        pendingSegments: segments.length,
        totalBytes: storageStats.totalBytes,
      });
    } catch (error) {
      console.error('[useOfflineMode] Failed to initialize:', error);
    }
  }, []);

  /**
   * Cleanup on unmount
   */
  const cleanup = useCallback(async () => {
    if (bufferUpdateIntervalRef.current) {
      clearInterval(bufferUpdateIntervalRef.current);
      bufferUpdateIntervalRef.current = null;
    }

    // Finalize any active buffer before closing
    if (isBufferActive()) {
      await finalizeBuffer();
    }

    await closeOfflineStorage();
    isInitializedRef.current = false;
    console.log('[useOfflineMode] Cleaned up');
  }, []);

  /**
   * Enter offline buffering mode
   */
  const enterOfflineMode = useCallback((sessionId: string, conversationId: string | null) => {
    if (isOffline) {
      console.log('[useOfflineMode] Already in offline mode');
      return;
    }

    currentSessionIdRef.current = sessionId;
    currentConversationIdRef.current = conversationId;

    startBuffer(sessionId, conversationId);
    setIsOffline(true);
    setIsBuffering(true);
    setLastActiveConversationId(conversationId);

    // Start interval to update buffer duration
    if (bufferUpdateIntervalRef.current) {
      clearInterval(bufferUpdateIntervalRef.current);
    }
    bufferUpdateIntervalRef.current = setInterval(() => {
      const bufferStats = getBufferStats();
      setCurrentBufferDurationMs(bufferStats.durationMs);
    }, 1000);

    console.log('[useOfflineMode] Entered offline mode', { sessionId, conversationId });
  }, [isOffline]);

  /**
   * Exit offline mode and finalize current buffer
   */
  const exitOfflineMode = useCallback(async (): Promise<PendingSegment | null> => {
    if (!isOffline) {
      console.log('[useOfflineMode] Not in offline mode');
      return null;
    }

    // Stop buffer duration updates
    if (bufferUpdateIntervalRef.current) {
      clearInterval(bufferUpdateIntervalRef.current);
      bufferUpdateIntervalRef.current = null;
    }

    // Finalize active buffer
    const finalSegment = await finalizeBuffer();

    setIsOffline(false);
    setIsBuffering(false);
    setCurrentBufferDurationMs(0);
    currentSessionIdRef.current = null;
    currentConversationIdRef.current = null;

    // Refresh stats after finalizing
    await refreshStats();
    await refreshPendingSegments();

    console.log('[useOfflineMode] Exited offline mode', {
      finalSegment: finalSegment?.id,
    });

    return finalSegment;
  }, [isOffline]);

  /**
   * Buffer an audio chunk while in offline mode
   * Returns segment if buffer was finalized (60 seconds reached)
   */
  const bufferAudioChunk = useCallback(async (chunk: Uint8Array): Promise<PendingSegment | null> => {
    if (!isOffline || !isBufferActive()) {
      console.warn('[useOfflineMode] Cannot buffer - not in offline mode');
      return null;
    }

    const segment = await addChunk(chunk);

    // If segment was finalized (60 seconds reached), rotate to new segment
    if (segment) {
      console.log('[useOfflineMode] Segment finalized, rotating buffer');

      // Rotate to new segment for continued buffering
      rotateBuffer(
        currentSessionIdRef.current!,
        currentConversationIdRef.current
      );

      // Update stats after segment finalization
      await refreshStats();
      await refreshPendingSegments();
    }

    return segment;
  }, [isOffline]);

  /**
   * Refresh pending segments list
   */
  const refreshPendingSegments = useCallback(async () => {
    try {
      const segments = await getPendingSegments();
      setPendingSegments(segments);
    } catch (error) {
      console.error('[useOfflineMode] Failed to refresh pending segments:', error);
    }
  }, []);

  /**
   * Refresh storage statistics
   */
  const refreshStats = useCallback(async () => {
    try {
      const storageStats = await getStorageStats();
      setStats(storageStats);
      setStorageWarning(storageStats.totalBytes >= STORAGE_WARNING_BYTES);
    } catch (error) {
      console.error('[useOfflineMode] Failed to refresh stats:', error);
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (bufferUpdateIntervalRef.current) {
        clearInterval(bufferUpdateIntervalRef.current);
      }
    };
  }, []);

  return {
    // State
    isOffline,
    isBuffering,
    pendingSegments,
    stats,
    currentBufferDurationMs,
    storageWarning,
    lastActiveConversationId,

    // Methods
    initialize,
    cleanup,
    enterOfflineMode,
    exitOfflineMode,
    bufferAudioChunk,
    refreshPendingSegments,
    refreshStats,
  };
};

export default useOfflineMode;
