/**
 * Offline Storage - SQLite + FileSystem abstraction for audio buffering
 *
 * Manages:
 * - SQLite database for segment metadata
 * - FileSystem directory for audio segment files
 * - CRUD operations for pending segments
 * - Auto-cleanup of old segments (7 days)
 */

import * as SQLite from 'expo-sqlite';
import * as FileSystem from 'expo-file-system/legacy';

// Constants
const DATABASE_NAME = 'chronicle_offline.db';
const AUDIO_SEGMENTS_DIR = 'offline_audio_segments';
const SEGMENT_RETENTION_DAYS = 7;
const CLEANUP_INTERVAL_MS = 6 * 60 * 60 * 1000; // 6 hours

export type SegmentStatus = 'pending' | 'uploading' | 'uploaded' | 'failed';

export interface PendingSegment {
  id: string;
  conversation_id: string | null;
  session_id: string;
  file_path: string;
  start_time: number; // Unix timestamp ms
  end_time: number;   // Unix timestamp ms
  byte_count: number;
  chunk_count: number;
  status: SegmentStatus;
  created_at: number; // Unix timestamp ms
  retry_count: number;
}

export interface OfflineStorageStats {
  totalSegments: number;
  pendingSegments: number;
  totalBytes: number;
  oldestSegmentAge: number | null; // ms
}

let db: SQLite.SQLiteDatabase | null = null;
let cleanupInterval: NodeJS.Timeout | null = null;

/**
 * Initialize the offline storage system
 */
export async function initOfflineStorage(): Promise<void> {
  // Open SQLite database
  db = await SQLite.openDatabaseAsync(DATABASE_NAME);

  // Create segments table if not exists
  await db.execAsync(`
    CREATE TABLE IF NOT EXISTS pending_segments (
      id TEXT PRIMARY KEY,
      conversation_id TEXT,
      session_id TEXT NOT NULL,
      file_path TEXT NOT NULL,
      start_time INTEGER NOT NULL,
      end_time INTEGER NOT NULL,
      byte_count INTEGER NOT NULL,
      chunk_count INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at INTEGER NOT NULL,
      retry_count INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_segments_status ON pending_segments(status);
    CREATE INDEX IF NOT EXISTS idx_segments_created_at ON pending_segments(created_at);
    CREATE INDEX IF NOT EXISTS idx_segments_session_id ON pending_segments(session_id);
  `);

  // Ensure audio segments directory exists
  const segmentsDir = getSegmentsDirectory();
  const dirInfo = await FileSystem.getInfoAsync(segmentsDir);
  if (!dirInfo.exists) {
    await FileSystem.makeDirectoryAsync(segmentsDir, { intermediates: true });
  }

  // Run initial cleanup
  await cleanupOldSegments();

  // Schedule periodic cleanup
  if (cleanupInterval) {
    clearInterval(cleanupInterval);
  }
  cleanupInterval = setInterval(cleanupOldSegments, CLEANUP_INTERVAL_MS);

  console.log('[OfflineStorage] Initialized');
}

/**
 * Get the directory path for audio segments
 */
export function getSegmentsDirectory(): string {
  return `${FileSystem.documentDirectory}${AUDIO_SEGMENTS_DIR}/`;
}

/**
 * Generate a unique file path for a new segment
 */
export function generateSegmentFilePath(sessionId: string): string {
  const timestamp = Date.now();
  const filename = `segment_${sessionId}_${timestamp}.wav`;
  return `${getSegmentsDirectory()}${filename}`;
}

/**
 * Save a new pending segment
 */
export async function savePendingSegment(segment: Omit<PendingSegment, 'created_at' | 'retry_count' | 'status'>): Promise<PendingSegment> {
  if (!db) throw new Error('OfflineStorage not initialized');

  const fullSegment: PendingSegment = {
    ...segment,
    status: 'pending',
    created_at: Date.now(),
    retry_count: 0,
  };

  await db.runAsync(
    `INSERT INTO pending_segments
     (id, conversation_id, session_id, file_path, start_time, end_time, byte_count, chunk_count, status, created_at, retry_count)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    [
      fullSegment.id,
      fullSegment.conversation_id,
      fullSegment.session_id,
      fullSegment.file_path,
      fullSegment.start_time,
      fullSegment.end_time,
      fullSegment.byte_count,
      fullSegment.chunk_count,
      fullSegment.status,
      fullSegment.created_at,
      fullSegment.retry_count,
    ]
  );

  console.log(`[OfflineStorage] Saved segment ${segment.id} (${segment.byte_count} bytes)`);
  return fullSegment;
}

/**
 * Get all pending segments (not yet uploaded)
 */
export async function getPendingSegments(): Promise<PendingSegment[]> {
  if (!db) throw new Error('OfflineStorage not initialized');

  const rows = await db.getAllAsync<PendingSegment>(
    `SELECT * FROM pending_segments
     WHERE status IN ('pending', 'failed')
     ORDER BY created_at ASC`
  );

  return rows;
}

/**
 * Get all segments for a specific session
 */
export async function getSegmentsBySession(sessionId: string): Promise<PendingSegment[]> {
  if (!db) throw new Error('OfflineStorage not initialized');

  const rows = await db.getAllAsync<PendingSegment>(
    `SELECT * FROM pending_segments
     WHERE session_id = ?
     ORDER BY start_time ASC`,
    [sessionId]
  );

  return rows;
}

/**
 * Update segment status
 */
export async function updateSegmentStatus(
  segmentId: string,
  status: SegmentStatus,
  incrementRetry: boolean = false
): Promise<void> {
  if (!db) throw new Error('OfflineStorage not initialized');

  if (incrementRetry) {
    await db.runAsync(
      `UPDATE pending_segments SET status = ?, retry_count = retry_count + 1 WHERE id = ?`,
      [status, segmentId]
    );
  } else {
    await db.runAsync(
      `UPDATE pending_segments SET status = ? WHERE id = ?`,
      [status, segmentId]
    );
  }

  console.log(`[OfflineStorage] Updated segment ${segmentId} status to ${status}`);
}

/**
 * Delete a segment and its audio file
 */
export async function deleteSegment(segmentId: string): Promise<void> {
  if (!db) throw new Error('OfflineStorage not initialized');

  // Get segment to find file path
  const segment = await db.getFirstAsync<PendingSegment>(
    `SELECT * FROM pending_segments WHERE id = ?`,
    [segmentId]
  );

  if (segment) {
    // Delete audio file
    try {
      const fileInfo = await FileSystem.getInfoAsync(segment.file_path);
      if (fileInfo.exists) {
        await FileSystem.deleteAsync(segment.file_path);
      }
    } catch (error) {
      console.warn(`[OfflineStorage] Failed to delete audio file: ${error}`);
    }

    // Delete database record
    await db.runAsync(`DELETE FROM pending_segments WHERE id = ?`, [segmentId]);
    console.log(`[OfflineStorage] Deleted segment ${segmentId}`);
  }
}

/**
 * Delete all uploaded segments (cleanup after successful sync)
 */
export async function deleteUploadedSegments(): Promise<number> {
  if (!db) throw new Error('OfflineStorage not initialized');

  // Get all uploaded segments
  const uploaded = await db.getAllAsync<PendingSegment>(
    `SELECT * FROM pending_segments WHERE status = 'uploaded'`
  );

  // Delete audio files
  for (const segment of uploaded) {
    try {
      const fileInfo = await FileSystem.getInfoAsync(segment.file_path);
      if (fileInfo.exists) {
        await FileSystem.deleteAsync(segment.file_path);
      }
    } catch (error) {
      console.warn(`[OfflineStorage] Failed to delete audio file: ${error}`);
    }
  }

  // Delete database records
  const result = await db.runAsync(`DELETE FROM pending_segments WHERE status = 'uploaded'`);
  console.log(`[OfflineStorage] Deleted ${result.changes} uploaded segments`);

  return result.changes;
}

/**
 * Cleanup segments older than retention period
 */
export async function cleanupOldSegments(): Promise<number> {
  if (!db) {
    console.warn('[OfflineStorage] Cannot cleanup - not initialized');
    return 0;
  }

  const cutoffTime = Date.now() - (SEGMENT_RETENTION_DAYS * 24 * 60 * 60 * 1000);

  // Get old segments
  const oldSegments = await db.getAllAsync<PendingSegment>(
    `SELECT * FROM pending_segments WHERE created_at < ?`,
    [cutoffTime]
  );

  // Delete audio files
  for (const segment of oldSegments) {
    try {
      const fileInfo = await FileSystem.getInfoAsync(segment.file_path);
      if (fileInfo.exists) {
        await FileSystem.deleteAsync(segment.file_path);
      }
    } catch (error) {
      console.warn(`[OfflineStorage] Failed to delete old audio file: ${error}`);
    }
  }

  // Delete database records
  const result = await db.runAsync(
    `DELETE FROM pending_segments WHERE created_at < ?`,
    [cutoffTime]
  );

  if (result.changes > 0) {
    console.log(`[OfflineStorage] Cleaned up ${result.changes} old segments`);
  }

  return result.changes;
}

/**
 * Get storage statistics
 */
export async function getStorageStats(): Promise<OfflineStorageStats> {
  if (!db) throw new Error('OfflineStorage not initialized');

  const stats = await db.getFirstAsync<{
    total: number;
    pending: number;
    bytes: number;
    oldest: number | null;
  }>(`
    SELECT
      COUNT(*) as total,
      SUM(CASE WHEN status IN ('pending', 'failed') THEN 1 ELSE 0 END) as pending,
      COALESCE(SUM(byte_count), 0) as bytes,
      MIN(created_at) as oldest
    FROM pending_segments
  `);

  return {
    totalSegments: stats?.total ?? 0,
    pendingSegments: stats?.pending ?? 0,
    totalBytes: stats?.bytes ?? 0,
    oldestSegmentAge: stats?.oldest ? Date.now() - stats.oldest : null,
  };
}

/**
 * Get last active conversation ID (for reconnection logic)
 */
export async function getLastActiveConversationId(): Promise<string | null> {
  if (!db) throw new Error('OfflineStorage not initialized');

  const result = await db.getFirstAsync<{ conversation_id: string | null }>(
    `SELECT conversation_id FROM pending_segments
     WHERE conversation_id IS NOT NULL
     ORDER BY created_at DESC
     LIMIT 1`
  );

  return result?.conversation_id ?? null;
}

/**
 * Close the database connection
 */
export async function closeOfflineStorage(): Promise<void> {
  if (cleanupInterval) {
    clearInterval(cleanupInterval);
    cleanupInterval = null;
  }

  if (db) {
    await db.closeAsync();
    db = null;
  }

  console.log('[OfflineStorage] Closed');
}

export default {
  init: initOfflineStorage,
  close: closeOfflineStorage,
  getSegmentsDirectory,
  generateSegmentFilePath,
  savePendingSegment,
  getPendingSegments,
  getSegmentsBySession,
  updateSegmentStatus,
  deleteSegment,
  deleteUploadedSegments,
  cleanupOldSegments,
  getStorageStats,
  getLastActiveConversationId,
};
