/**
 * Audio Buffer - Handles buffering audio chunks to 60-second WAV segment files
 *
 * Manages:
 * - Accumulating PCM audio chunks
 * - Writing WAV files when segment is complete
 * - Tracking segment metadata
 */

import * as FileSystem from 'expo-file-system/legacy';
import {
  PendingSegment,
  generateSegmentFilePath,
  savePendingSegment,
} from './offlineStorage';

// Audio format constants (matching OMI device output)
const SAMPLE_RATE = 16000;
const BITS_PER_SAMPLE = 16;
const NUM_CHANNELS = 1;
const BYTES_PER_SAMPLE = BITS_PER_SAMPLE / 8;

// Segment configuration
const SEGMENT_DURATION_MS = 60000; // 60 seconds
const MAX_SEGMENT_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * NUM_CHANNELS * (SEGMENT_DURATION_MS / 1000);

interface ActiveBuffer {
  sessionId: string;
  conversationId: string | null;
  filePath: string;
  chunks: Uint8Array[];
  totalBytes: number;
  chunkCount: number;
  startTime: number;
}

let activeBuffer: ActiveBuffer | null = null;

/**
 * Generate a unique segment ID
 */
function generateSegmentId(): string {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * Create WAV file header
 */
function createWavHeader(dataLength: number): Uint8Array {
  const header = new ArrayBuffer(44);
  const view = new DataView(header);

  const byteRate = SAMPLE_RATE * NUM_CHANNELS * BYTES_PER_SAMPLE;
  const blockAlign = NUM_CHANNELS * BYTES_PER_SAMPLE;

  // RIFF header
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataLength, true); // File size - 8
  writeString(view, 8, 'WAVE');

  // fmt subchunk
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true); // Subchunk1Size (PCM = 16)
  view.setUint16(20, 1, true);  // AudioFormat (PCM = 1)
  view.setUint16(22, NUM_CHANNELS, true);
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, BITS_PER_SAMPLE, true);

  // data subchunk
  writeString(view, 36, 'data');
  view.setUint32(40, dataLength, true);

  return new Uint8Array(header);
}

/**
 * Helper to write string to DataView
 */
function writeString(view: DataView, offset: number, string: string): void {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

/**
 * Start a new buffer for offline audio
 */
export function startBuffer(sessionId: string, conversationId: string | null): void {
  if (activeBuffer) {
    console.warn('[AudioBuffer] Buffer already active, finalizing previous');
    // Don't await - just trigger finalization
    finalizeBuffer().catch(console.error);
  }

  activeBuffer = {
    sessionId,
    conversationId,
    filePath: generateSegmentFilePath(sessionId),
    chunks: [],
    totalBytes: 0,
    chunkCount: 0,
    startTime: Date.now(),
  };

  console.log(`[AudioBuffer] Started buffer for session ${sessionId}`);
}

/**
 * Add audio chunk to the buffer
 * Returns the saved segment if buffer is finalized, null otherwise
 */
export async function addChunk(chunk: Uint8Array): Promise<PendingSegment | null> {
  if (!activeBuffer) {
    console.warn('[AudioBuffer] No active buffer, dropping chunk');
    return null;
  }

  activeBuffer.chunks.push(chunk);
  activeBuffer.totalBytes += chunk.length;
  activeBuffer.chunkCount++;

  // Check if segment is full
  if (activeBuffer.totalBytes >= MAX_SEGMENT_BYTES) {
    return await finalizeBuffer();
  }

  return null;
}

/**
 * Finalize the current buffer and write WAV file
 */
export async function finalizeBuffer(): Promise<PendingSegment | null> {
  if (!activeBuffer || activeBuffer.totalBytes === 0) {
    console.log('[AudioBuffer] No active buffer or empty buffer');
    activeBuffer = null;
    return null;
  }

  const buffer = activeBuffer;
  activeBuffer = null;

  try {
    // Concatenate all chunks
    const totalLength = buffer.chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const audioData = new Uint8Array(totalLength);
    let offset = 0;
    for (const chunk of buffer.chunks) {
      audioData.set(chunk, offset);
      offset += chunk.length;
    }

    // Create WAV file with header
    const header = createWavHeader(totalLength);
    const wavData = new Uint8Array(header.length + audioData.length);
    wavData.set(header, 0);
    wavData.set(audioData, header.length);

    // Write to file (base64 encoding for FileSystem)
    const base64Data = uint8ArrayToBase64(wavData);
    await FileSystem.writeAsStringAsync(buffer.filePath, base64Data, {
      encoding: FileSystem.EncodingType.Base64,
    });

    // Save segment metadata
    const segment = await savePendingSegment({
      id: generateSegmentId(),
      conversation_id: buffer.conversationId,
      session_id: buffer.sessionId,
      file_path: buffer.filePath,
      start_time: buffer.startTime,
      end_time: Date.now(),
      byte_count: wavData.length,
      chunk_count: buffer.chunkCount,
    });

    console.log(`[AudioBuffer] Finalized segment ${segment.id} (${wavData.length} bytes, ${buffer.chunkCount} chunks)`);
    return segment;
  } catch (error) {
    console.error('[AudioBuffer] Failed to finalize buffer:', error);
    return null;
  }
}

/**
 * Check if buffer is active
 */
export function isBufferActive(): boolean {
  return activeBuffer !== null;
}

/**
 * Get current buffer stats
 */
export function getBufferStats(): {
  isActive: boolean;
  sessionId: string | null;
  conversationId: string | null;
  totalBytes: number;
  chunkCount: number;
  durationMs: number;
} {
  if (!activeBuffer) {
    return {
      isActive: false,
      sessionId: null,
      conversationId: null,
      totalBytes: 0,
      chunkCount: 0,
      durationMs: 0,
    };
  }

  return {
    isActive: true,
    sessionId: activeBuffer.sessionId,
    conversationId: activeBuffer.conversationId,
    totalBytes: activeBuffer.totalBytes,
    chunkCount: activeBuffer.chunkCount,
    durationMs: Date.now() - activeBuffer.startTime,
  };
}

/**
 * Cancel and discard the current buffer
 */
export function cancelBuffer(): void {
  if (activeBuffer) {
    console.log(`[AudioBuffer] Cancelled buffer for session ${activeBuffer.sessionId}`);
    activeBuffer = null;
  }
}

/**
 * Update conversation ID for current buffer (e.g., after reconnection)
 */
export function updateBufferConversationId(conversationId: string): void {
  if (activeBuffer) {
    activeBuffer.conversationId = conversationId;
    console.log(`[AudioBuffer] Updated conversation ID to ${conversationId}`);
  }
}

/**
 * Start a new segment while keeping the same session
 * Used when a segment is finalized but recording continues
 */
export function rotateBuffer(sessionId: string, conversationId: string | null): void {
  activeBuffer = {
    sessionId,
    conversationId,
    filePath: generateSegmentFilePath(sessionId),
    chunks: [],
    totalBytes: 0,
    chunkCount: 0,
    startTime: Date.now(),
  };

  console.log(`[AudioBuffer] Rotated to new segment for session ${sessionId}`);
}

/**
 * Convert Uint8Array to base64 string
 */
function uint8ArrayToBase64(bytes: Uint8Array): string {
  let binary = '';
  const len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

export default {
  startBuffer,
  addChunk,
  finalizeBuffer,
  isBufferActive,
  getBufferStats,
  cancelBuffer,
  updateBufferConversationId,
  rotateBuffer,
};
