/**
 * Offline Sync Service - Handles uploading buffered audio when connection is restored
 *
 * Manages:
 * - Checking if conversation is still open
 * - Uploading pending segments to server
 * - Retry logic with exponential backoff
 * - Progress tracking
 */

import * as FileSystem from 'expo-file-system/legacy';
import {
  getPendingSegments,
  updateSegmentStatus,
  deleteUploadedSegments,
  PendingSegment,
} from '../storage/offlineStorage';

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

export interface SyncProgress {
  total: number;
  completed: number;
  failed: number;
  inProgress: boolean;
  currentSegmentId: string | null;
}

export interface ConversationStatus {
  isOpen: boolean;
  endReason: string | null;
  completedAt: string | null;
}

export interface SyncResult {
  success: boolean;
  uploaded: number;
  failed: number;
  errors: string[];
}

type ProgressCallback = (progress: SyncProgress) => void;

/**
 * Check if a conversation is still open on the server
 */
export async function checkConversationStatus(
  baseUrl: string,
  conversationId: string,
  jwtToken: string
): Promise<ConversationStatus> {
  try {
    const response = await fetch(`${baseUrl}/api/conversations/${conversationId}`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${jwtToken}`,
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      if (response.status === 404) {
        // Conversation not found - treat as closed
        return { isOpen: false, endReason: 'not_found', completedAt: null };
      }
      throw new Error(`Failed to check conversation status: ${response.status}`);
    }

    const data = await response.json();

    // If end_reason and completed_at are both null, conversation is still open
    const isOpen = !data.end_reason && !data.completed_at;

    return {
      isOpen,
      endReason: data.end_reason || null,
      completedAt: data.completed_at || null,
    };
  } catch (error) {
    console.error('[OfflineSync] Error checking conversation status:', error);
    // On error, assume conversation is closed to be safe
    return { isOpen: false, endReason: 'error', completedAt: null };
  }
}

/**
 * Upload a single segment file to the server
 */
async function uploadSegment(
  baseUrl: string,
  segment: PendingSegment,
  jwtToken: string
): Promise<boolean> {
  try {
    // Read the file
    const fileInfo = await FileSystem.getInfoAsync(segment.file_path);
    if (!fileInfo.exists) {
      console.warn(`[OfflineSync] Segment file not found: ${segment.file_path}`);
      return false;
    }

    // Read file as base64
    const base64Content = await FileSystem.readAsStringAsync(segment.file_path, {
      encoding: FileSystem.EncodingType.Base64,
    });

    // Create form data for upload
    const formData = new FormData();

    // Convert base64 to blob for upload
    const blob = base64ToBlob(base64Content, 'audio/wav');
    const filename = segment.file_path.split('/').pop() || 'segment.wav';

    formData.append('files', blob, filename);
    formData.append('device_name', 'offline_upload');

    // Upload to server
    const response = await fetch(`${baseUrl}/api/audio/upload`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${jwtToken}`,
      },
      body: formData,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Upload failed: ${response.status} - ${errorText}`);
    }

    const result = await response.json();
    console.log(`[OfflineSync] Uploaded segment ${segment.id}:`, result);

    return true;
  } catch (error) {
    console.error(`[OfflineSync] Failed to upload segment ${segment.id}:`, error);
    return false;
  }
}

/**
 * Convert base64 string to Blob
 */
function base64ToBlob(base64: string, mimeType: string): Blob {
  const byteCharacters = atob(base64);
  const byteNumbers = new Array(byteCharacters.length);

  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }

  const byteArray = new Uint8Array(byteNumbers);
  return new Blob([byteArray], { type: mimeType });
}

/**
 * Sync all pending segments to the server
 */
export async function syncPendingSegments(
  baseUrl: string,
  jwtToken: string,
  onProgress?: ProgressCallback
): Promise<SyncResult> {
  const segments = await getPendingSegments();

  if (segments.length === 0) {
    console.log('[OfflineSync] No pending segments to sync');
    return { success: true, uploaded: 0, failed: 0, errors: [] };
  }

  console.log(`[OfflineSync] Starting sync of ${segments.length} segments`);

  const errors: string[] = [];
  let uploaded = 0;
  let failed = 0;

  const progress: SyncProgress = {
    total: segments.length,
    completed: 0,
    failed: 0,
    inProgress: true,
    currentSegmentId: null,
  };

  onProgress?.(progress);

  for (const segment of segments) {
    progress.currentSegmentId = segment.id;
    onProgress?.(progress);

    // Update status to uploading
    await updateSegmentStatus(segment.id, 'uploading');

    let success = false;
    let retries = 0;

    // Retry loop
    while (!success && retries < MAX_RETRIES) {
      if (retries > 0) {
        // Wait before retry with exponential backoff
        await new Promise(resolve =>
          setTimeout(resolve, RETRY_DELAY_MS * Math.pow(2, retries - 1))
        );
      }

      success = await uploadSegment(baseUrl, segment, jwtToken);
      retries++;
    }

    if (success) {
      await updateSegmentStatus(segment.id, 'uploaded');
      uploaded++;
      progress.completed++;
    } else {
      await updateSegmentStatus(segment.id, 'failed', true);
      failed++;
      progress.failed++;
      errors.push(`Failed to upload segment ${segment.id} after ${MAX_RETRIES} attempts`);
    }

    onProgress?.(progress);
  }

  progress.inProgress = false;
  progress.currentSegmentId = null;
  onProgress?.(progress);

  // Cleanup uploaded segments
  if (uploaded > 0) {
    await deleteUploadedSegments();
  }

  console.log(`[OfflineSync] Sync complete: ${uploaded} uploaded, ${failed} failed`);

  return {
    success: failed === 0,
    uploaded,
    failed,
    errors,
  };
}

/**
 * Handle reconnection logic
 * Checks if last conversation is still open, decides whether to resume or upload as new
 */
export async function handleReconnection(
  baseUrl: string,
  jwtToken: string,
  lastConversationId: string | null,
  onProgress?: ProgressCallback
): Promise<{
  action: 'resume' | 'upload_as_new' | 'no_action';
  conversationId?: string;
  syncResult?: SyncResult;
}> {
  const segments = await getPendingSegments();

  if (segments.length === 0) {
    console.log('[OfflineSync] No pending segments, no action needed');
    return { action: 'no_action' };
  }

  // Check if last conversation is still open
  if (lastConversationId) {
    const status = await checkConversationStatus(baseUrl, lastConversationId, jwtToken);

    if (status.isOpen) {
      console.log(`[OfflineSync] Conversation ${lastConversationId} is still open`);
      // Resume streaming - segments will be handled by WebSocket
      // Note: actual resumption is handled by the WebSocket reconnect logic
      return { action: 'resume', conversationId: lastConversationId };
    }

    console.log(`[OfflineSync] Conversation ${lastConversationId} is closed (${status.endReason})`);
  }

  // Upload pending segments as new audio files
  console.log('[OfflineSync] Uploading pending segments as new audio');
  const syncResult = await syncPendingSegments(baseUrl, jwtToken, onProgress);

  return { action: 'upload_as_new', syncResult };
}

export default {
  checkConversationStatus,
  syncPendingSegments,
  handleReconnection,
};
