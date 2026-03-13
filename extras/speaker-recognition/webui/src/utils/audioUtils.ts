/**
 * Audio processing utilities for WAV creation and audio buffer manipulation
 */

export interface AudioSegment {
  start: number
  end: number
  duration: number
  speaker?: string
  text?: string
}

/**
 * Creates a WAV header for the given audio parameters
 */
export function createWAVHeader(
  sampleRate: number,
  channels: number,
  bitsPerSample: number,
  dataLength: number
): ArrayBuffer {
  const buffer = new ArrayBuffer(44)
  const view = new DataView(buffer)

  // WAV file header
  const writeString = (offset: number, string: string) => {
    for (let i = 0; i < string.length; i++) {
      view.setUint8(offset + i, string.charCodeAt(i))
    }
  }

  writeString(0, 'RIFF')
  view.setUint32(4, 36 + dataLength, true)
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, channels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * channels * (bitsPerSample / 8), true)
  view.setUint16(32, channels * (bitsPerSample / 8), true)
  view.setUint16(34, bitsPerSample, true)
  writeString(36, 'data')
  view.setUint32(40, dataLength, true)

  return buffer
}

/**
 * Creates a WAV blob from Float32Array audio buffer
 */
export function createWAVBlob(audioBuffer: Float32Array, sampleRate: number): Blob {
  const length = audioBuffer.length
  const arrayBuffer = new ArrayBuffer(length * 2)
  const view = new DataView(arrayBuffer)

  // Convert to 16-bit PCM
  for (let i = 0; i < length; i++) {
    const sample = Math.max(-1, Math.min(1, audioBuffer[i]))
    view.setInt16(i * 2, sample * 0x7FFF, true)
  }

  // Create WAV blob
  return new Blob([createWAVHeader(sampleRate, 1, 16, arrayBuffer.byteLength), arrayBuffer], {
    type: 'audio/wav'
  })
}

/**
 * Concatenates multiple Float32Array buffers into a single buffer
 */
export function concatenateAudioBuffers(buffers: Float32Array[]): Float32Array {
  const totalLength = buffers.reduce((sum, buf) => sum + buf.length, 0)
  const result = new Float32Array(totalLength)

  let offset = 0
  for (const buffer of buffers) {
    result.set(buffer, offset)
    offset += buffer.length
  }

  return result
}

/**
 * Calculates buffer indices based on timing information
 */
export function calculateBufferIndices(
  utteranceStartTime: number,
  utteranceEndTime: number,
  bufferDurationMs: number = 256,
  maxBuffers: number
): { startIndex: number; endIndex: number; isValid: boolean } {
  // Calculate buffer indices based on stream-relative timing
  const startBufferIndex = Math.max(0, Math.floor(utteranceStartTime * 1000 / bufferDurationMs))
  const endBufferIndex = Math.min(maxBuffers, Math.ceil(utteranceEndTime * 1000 / bufferDurationMs))

  const isValid = startBufferIndex < endBufferIndex && startBufferIndex < maxBuffers

  return {
    startIndex: startBufferIndex,
    endIndex: endBufferIndex,
    isValid
  }
}

/**
 * Extracts audio segment from buffer array based on indices
 */
export function extractAudioSegmentFromBuffers(
  audioBuffers: Float32Array[],
  startIndex: number,
  endIndex: number
): Float32Array | null {
  if (startIndex >= endIndex || startIndex >= audioBuffers.length) {
    return null
  }

  const segmentBuffers = audioBuffers.slice(startIndex, endIndex)

  if (segmentBuffers.length === 0) {
    return null
  }

  return concatenateAudioBuffers(segmentBuffers)
}

/**
 * Validates audio configuration parameters
 */
export function validateAudioConfig(config: {
  sampleRate?: number
  channels?: number
  bufferSize?: number
}): { isValid: boolean; errors: string[] } {
  const errors: string[] = []

  if (config.sampleRate && (config.sampleRate < 8000 || config.sampleRate > 48000)) {
    errors.push('Sample rate must be between 8000 and 48000 Hz')
  }

  if (config.channels && (config.channels < 1 || config.channels > 2)) {
    errors.push('Channels must be 1 (mono) or 2 (stereo)')
  }

  if (config.bufferSize && ![256, 512, 1024, 2048, 4096, 8192, 16384].includes(config.bufferSize)) {
    errors.push('Buffer size must be a power of 2 between 256 and 16384')
  }

  return {
    isValid: errors.length === 0,
    errors
  }
}

/**
 * Loads audio file and returns ArrayBuffer
 */
export async function loadAudioBuffer(file: File): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(reader.result)
      } else {
        reject(new Error('Failed to read file as ArrayBuffer'))
      }
    }
    reader.onerror = () => reject(new Error('Failed to read file'))
    reader.readAsArrayBuffer(file)
  })
}

/**
 * Creates an AudioContext with fallback for older browsers
 */
export function createAudioContext(options?: AudioContextOptions): AudioContext {
  const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext
  return new AudioContextClass(options)
}

/**
 * Decodes audio data to AudioBuffer
 */
export async function decodeAudioData(audioContext: AudioContext, arrayBuffer: ArrayBuffer): Promise<AudioBuffer> {
  return new Promise((resolve, reject) => {
    audioContext.decodeAudioData(
      arrayBuffer,
      (audioBuffer) => resolve(audioBuffer),
      (error) => reject(error)
    )
  })
}

/**
 * Extracts audio samples from AudioBuffer as Float32Array
 */
export function extractAudioSamples(audioBuffer: AudioBuffer): Float32Array {
  // Mix down to mono if stereo
  const channelData = audioBuffer.getChannelData(0)

  if (audioBuffer.numberOfChannels === 1) {
    return new Float32Array(channelData)
  }

  // Mix stereo to mono
  const leftChannel = audioBuffer.getChannelData(0)
  const rightChannel = audioBuffer.getChannelData(1)
  const mono = new Float32Array(leftChannel.length)

  for (let i = 0; i < leftChannel.length; i++) {
    mono[i] = (leftChannel[i] + rightChannel[i]) / 2
  }

  return mono
}

/**
 * Calculates Signal-to-Noise Ratio (SNR) of audio samples
 */
export function calculateSNR(samples: Float32Array): number {
  if (samples.length === 0) return 0

  // Calculate RMS (Root Mean Square) for signal power
  let sumSquares = 0
  for (let i = 0; i < samples.length; i++) {
    sumSquares += samples[i] * samples[i]
  }
  const rms = Math.sqrt(sumSquares / samples.length)

  // Estimate noise floor (assume lowest 10% of values represent noise)
  const sorted = Array.from(samples).map(Math.abs).sort((a, b) => a - b)
  const noiseFloorIndex = Math.floor(sorted.length * 0.1)
  const noiseFloor = sorted[noiseFloorIndex] || 0.001 // Avoid division by zero

  // SNR in dB
  const snr = 20 * Math.log10(rms / noiseFloor)
  return isFinite(snr) ? snr : 0
}

/**
 * Creates audio blob from samples
 */
export function createAudioBlob(samples: Float32Array, sampleRate: number = 44100): Blob {
  return createWAVBlob(samples, sampleRate)
}

/**
 * Converts any audio blob to WAV format
 */
export async function convertBlobToWav(blob: Blob): Promise<Blob> {
  // If already WAV, return as-is
  if (blob.type.includes('wav')) {
    return blob
  }

  // Otherwise, decode and re-encode as WAV
  const arrayBuffer = await blob.arrayBuffer()
  const audioContext = createAudioContext()
  const audioBuffer = await decodeAudioData(audioContext, arrayBuffer)
  const samples = extractAudioSamples(audioBuffer)

  return createWAVBlob(samples, audioBuffer.sampleRate)
}

/**
 * Extracts a segment of audio from samples
 */
export function extractAudioSegment(
  samples: Float32Array,
  startTime: number,
  endTime: number,
  sampleRate: number
): Float32Array {
  // Validate inputs
  if (!(samples instanceof Float32Array)) {
    throw new Error(`extractAudioSegment: Expected Float32Array, got ${(samples as any)?.constructor?.name || typeof samples}. Use extractAudioSamples() to convert AudioBuffer first.`)
  }
  if (!sampleRate || sampleRate <= 0 || !isFinite(sampleRate)) {
    throw new Error(`extractAudioSegment: Invalid sampleRate: ${sampleRate}`)
  }
  if (!isFinite(startTime) || !isFinite(endTime) || startTime < 0 || endTime < 0) {
    throw new Error(`extractAudioSegment: Invalid time values: startTime=${startTime}s, endTime=${endTime}s`)
  }
  if (startTime >= endTime) {
    throw new Error(`extractAudioSegment: Invalid time range: startTime (${startTime}s) must be less than endTime (${endTime}s)`)
  }
  if (samples.length === 0) {
    throw new Error(`extractAudioSegment: Empty samples array provided`)
  }

  const startSample = Math.floor(startTime * sampleRate)
  const endSample = Math.floor(endTime * sampleRate)

  // Validate calculated sample indices
  if (startSample >= samples.length) {
    throw new Error(`extractAudioSegment: Start time ${startTime}s (sample ${startSample}) is beyond audio length ${samples.length / sampleRate}s (${samples.length} samples)`)
  }

  const segmentLength = Math.max(0, Math.min(endSample - startSample, samples.length - startSample))

  if (segmentLength === 0) {
    console.warn(`extractAudioSegment: Calculated segment length is 0 for time range ${startTime}s-${endTime}s`)
  }

  const segment = new Float32Array(segmentLength)

  for (let i = 0; i < segmentLength; i++) {
    segment[i] = samples[startSample + i] || 0
  }

  return segment
}

/**
 * Formats duration in milliseconds to human-readable string
 * @deprecated Use formatDuration from utils/common instead
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
