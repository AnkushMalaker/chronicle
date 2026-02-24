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
