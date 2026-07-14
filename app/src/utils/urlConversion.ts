/**
 * Converts an HTTP(S) URL to the corresponding WebSocket URL for the Chronicle backend.
 *
 * Examples:
 *   https://100.64.1.5       → wss://100.64.1.5/ws
 *   http://localhost:8000     → ws://localhost:8000/ws
 *   https://my.server.com    → wss://my.server.com/ws
 */
export function httpUrlToWebSocketUrl(httpUrl: string): string {
  let url = httpUrl.trim().replace(/\/+$/, '')

  if (url.startsWith('https://')) {
    url = 'wss://' + url.slice('https://'.length)
  } else if (url.startsWith('http://')) {
    url = 'ws://' + url.slice('http://'.length)
  } else {
    // If no scheme, assume wss
    url = 'wss://' + url
  }

  // Append /ws if not already present
  if (!url.endsWith('/ws')) {
    url += '/ws'
  }

  return url
}

/** A backend configuration parsed from a scanned QR code. */
export interface ScannedBackendConfig {
  /** HTTP(S) base URL of the backend. */
  backendUrl: string
  /** Optional service-manager URL (lets the app start a down backend). */
  serviceManagerUrl?: string
  /** Optional service-manager bearer token. */
  smToken?: string
}

/**
 * Parse a scanned QR payload. Supports two formats:
 *   1. A bare HTTP(S) URL (legacy).
 *   2. A JSON bundle: { backendUrl, serviceManagerUrl?, smToken? }.
 * Returns null if the payload contains no valid backend URL.
 */
export function parseScannedConfig(data: string): ScannedBackendConfig | null {
  const trimmed = (data || '').trim()
  if (!trimmed) return null

  // Try JSON bundle first.
  if (trimmed.startsWith('{')) {
    try {
      const obj = JSON.parse(trimmed)
      if (obj && typeof obj.backendUrl === 'string' && isValidBackendUrl(obj.backendUrl)) {
        return {
          backendUrl: obj.backendUrl.trim(),
          serviceManagerUrl:
            typeof obj.serviceManagerUrl === 'string' ? obj.serviceManagerUrl.trim() : undefined,
          smToken: typeof obj.smToken === 'string' ? obj.smToken : undefined,
        }
      }
      return null
    } catch {
      return null
    }
  }

  // Fall back to a plain URL.
  if (isValidBackendUrl(trimmed)) {
    return { backendUrl: trimmed }
  }
  return null
}

/**
 * Validates that a scanned string looks like a valid HTTP(S) backend URL.
 */
export function isValidBackendUrl(url: string): boolean {
  if (!url || typeof url !== 'string') return false

  const trimmed = url.trim()
  if (!trimmed.startsWith('http://') && !trimmed.startsWith('https://')) {
    return false
  }

  try {
    const parsed = new URL(trimmed)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}
