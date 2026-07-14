/**
 * Shared audio-format detection for playback.
 *
 * The backend can serve conversation audio as ogg/opus (small) or wav (universal).
 * We prefer opus where the browser can decode it, and fall back to wav otherwise.
 *
 * Two layers:
 * - `AUDIO_FORMAT` / `SUPPORTS_OPUS` — a static snapshot taken at module load, for
 *   non-WebAudio uses like the download button (it just needs a stable choice).
 * - `decodeFormat()` / `demoteToWav()` — the *runtime* format used by the Web Audio
 *   scheduler. Safari advertises opus support but then throws inside
 *   `decodeAudioData` for ogg/opus, so the scheduler permanently demotes to wav on
 *   the first decode failure.
 */

export const SUPPORTS_OPUS = (() => {
  try {
    const a = document.createElement('audio')
    return a.canPlayType('audio/ogg; codecs=opus') !== ''
  } catch {
    return false
  }
})()

// Static snapshot for non-WebAudio consumers (download buttons, etc.).
export const AUDIO_FORMAT: 'opus' | 'wav' = SUPPORTS_OPUS ? 'opus' : 'wav'

// Runtime format used by the Web Audio scheduler — may be demoted to 'wav'.
let runtimeFormat: 'opus' | 'wav' = AUDIO_FORMAT

export function decodeFormat(): 'opus' | 'wav' {
  return runtimeFormat
}

export function demoteToWav(): void {
  runtimeFormat = 'wav'
}
