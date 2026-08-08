// Uploading a screenshot the user shared into Chronicle.
//
// The share extension only hands the image to this app; the upload happens here so
// it can reuse the JWT already in secure storage. That is why no keychain access
// group is needed — the extension never authenticates.

import * as ImageManipulator from 'expo-image-manipulator';

import { deriveBaseUrl, fetchAuthed } from './auth';
import { logError, logInfo } from '@/utils/logger';

// Wide enough to keep small text legible for the describer, small enough that a
// share is a fast upload on mobile data.
const MAX_WIDTH = 2048;
const JPEG_QUALITY = 0.85;

export interface ShareUploadResult {
  status: 'accepted' | 'duplicate';
  itemId: string;
  contentHash: string;
}

/**
 * Normalise to JPEG before upload.
 *
 * iOS shares HEIC, which the backend deliberately rejects rather than transcode —
 * it has no image library, so the conversion has to happen on the device that
 * already has one. Resizing also keeps every share well under the 10 MiB cap.
 */
async function toUploadableJpeg(uri: string): Promise<string> {
  const result = await ImageManipulator.manipulateAsync(
    uri,
    [{ resize: { width: MAX_WIDTH } }],
    { compress: JPEG_QUALITY, format: ImageManipulator.SaveFormat.JPEG }
  );
  return result.uri;
}

export async function uploadSharedScreenshot(
  uri: string,
  webSocketUrl: string,
  options: { caption?: string; capturedAt?: Date } = {}
): Promise<ShareUploadResult> {
  const baseUrl = deriveBaseUrl(webSocketUrl);
  if (!baseUrl) {
    throw new Error('No backend configured. Set the backend URL in Settings first.');
  }

  const jpegUri = await toUploadableJpeg(uri);
  const form = new FormData();
  // React Native's FormData takes this shape for a file part; do NOT set a
  // Content-Type header anywhere, or the multipart boundary is lost.
  form.append('file', {
    uri: jpegUri,
    name: 'screenshot.jpg',
    type: 'image/jpeg',
  } as unknown as Blob);
  form.append('captured_at', (options.capturedAt ?? new Date()).toISOString());
  if (options.caption?.trim()) {
    form.append('caption', options.caption.trim());
  }

  const response = await fetchAuthed(`${baseUrl}/api/device-input/screenshots`, {
    method: 'POST',
    body: form,
  });

  if (!response.ok) {
    let detail = `Upload failed (${response.status})`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      // Non-JSON error body; the status line is all we can report.
    }
    logError('Screenshots', `upload failed: ${detail}`);
    throw new Error(detail);
  }

  const body = await response.json();
  logInfo('Screenshots', `upload ${body.status} (${body.item_id})`);
  return {
    status: body.status,
    itemId: body.item_id,
    contentHash: body.content_hash,
  };
}
