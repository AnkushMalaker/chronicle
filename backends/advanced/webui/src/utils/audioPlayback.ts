/**
 * Plays backend→device "downlink" audio in the browser.
 *
 * The backend forwards wake-word tones and Hermes TTS replies down the recording
 * WebSocket as `play-audio` frames: `{ type: 'play-audio', data: { audio_b64, format } }`.
 * Both tones and TTS arrive as base64-encoded audio (the wake-word service sends its
 * tones inline as `play-audio` too), so a single handler covers both.
 *
 * Mirrors the React Native app's `app/src/utils/audioPlayback.ts`, adapted to the
 * browser (Blob URL + HTMLAudioElement instead of expo-file-system/expo-audio).
 */

export interface DownlinkAudioData {
  audio_b64?: string
  format?: string
  announcement?: boolean
}

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64)
  const buffer = new ArrayBuffer(binary.length)
  const bytes = new Uint8Array(buffer)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i)
  }
  return buffer
}

const MIME_BY_FORMAT: Record<string, string> = {
  wav: 'audio/wav',
  mp3: 'audio/mpeg',
  ogg: 'audio/ogg',
  flac: 'audio/flac',
}

/**
 * Decode and play a `play-audio` downlink frame's data through the default output.
 *
 * Best-effort: a malformed payload or a blocked autoplay just produces no sound and
 * is logged, never thrown — audio output must never break the recording session.
 * The mic stream uses echo cancellation, so playing a short tone/TTS while recording
 * generally won't feed back into the captured audio.
 */
export function playDownlinkAudio(data: DownlinkAudioData | undefined): void {
  if (!data?.audio_b64) return
  try {
    const buffer = base64ToArrayBuffer(data.audio_b64)
    const mime = MIME_BY_FORMAT[(data.format || 'wav').toLowerCase()] || 'audio/wav'
    const url = URL.createObjectURL(new Blob([buffer], { type: mime }))
    const audio = new Audio(url)
    const cleanup = () => URL.revokeObjectURL(url)
    audio.onended = cleanup
    audio.onerror = cleanup
    audio.play().catch((err) => {
      cleanup()
      console.warn('Downlink audio playback blocked:', err)
    })
  } catch (err) {
    console.warn('Failed to play downlink audio:', err)
  }
}
