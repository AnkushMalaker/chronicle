/**
 * Common utility functions used across the application
 */

/**
 * Formats duration in milliseconds to human-readable string
 */
export function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000)
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)

  if (hours > 0) {
    return `${hours}:${(minutes % 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
  }
  return `${minutes}:${(seconds % 60).toString().padStart(2, '0')}`
}

/**
 * Validates Deepgram API key format
 */
export function validateApiKey(key: string): { isValid: boolean; error?: string } {
  if (!key || !key.trim()) {
    return { isValid: false, error: 'API key is required' }
  }

  if (!key.match(/^[a-f0-9]{40}$/i)) {
    return { isValid: false, error: 'Invalid API key format. Should be a 40-character hex string.' }
  }

  return { isValid: true }
}

/**
 * Generates unique segment ID
 */
export function generateSegmentId(counter: number): string {
  return `segment_${counter}`
}

/**
 * Safe array access with bounds checking
 */
export function safeArrayAccess<T>(array: T[], index: number): T | undefined {
  return index >= 0 && index < array.length ? array[index] : undefined
}

/**
 * Debounce function to limit frequent calls
 */
export function debounce<T extends (...args: any[]) => any>(
  func: T,
  delay: number
): (...args: Parameters<T>) => void {
  let timeoutId: NodeJS.Timeout | null = null

  return (...args: Parameters<T>) => {
    if (timeoutId) {
      clearTimeout(timeoutId)
    }

    timeoutId = setTimeout(() => {
      func(...args)
    }, delay)
  }
}

/**
 * Creates a promise that resolves after specified delay
 */
export function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/**
 * Type guard to check if error is an Error instance
 */
export function isError(error: unknown): error is Error {
  return error instanceof Error
}

/**
 * Safe error message extraction
 */
export function getErrorMessage(error: unknown): string {
  if (isError(error)) {
    return error.message
  }
  return String(error)
}

/**
 * Creates a safe clone of an object (JSON-based)
 */
export function safeClone<T>(obj: T): T {
  try {
    return JSON.parse(JSON.stringify(obj))
  } catch {
    return obj
  }
}

/**
 * Checks if two arrays are equal (shallow comparison)
 */
export function arrayEquals<T>(a: T[], b: T[]): boolean {
  if (a.length !== b.length) return false
  return a.every((val, index) => val === b[index])
}

/**
 * Random string generator for unique IDs
 */
export function generateRandomId(length: number = 8): string {
  return Math.random().toString(36).substring(2, 2 + length)
}

/**
 * Safe number parsing with default value
 */
export function safeParseNumber(value: string | number, defaultValue: number = 0): number {
  if (typeof value === 'number') return value
  const parsed = parseFloat(value)
  return isNaN(parsed) ? defaultValue : parsed
}

/**
 * Generates an opaque speaker id. The owning tenant is the `user_id` field on
 * the speaker record, never part of the id.
 */
export function newSpeakerId(): string {
  return `speaker_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
}
