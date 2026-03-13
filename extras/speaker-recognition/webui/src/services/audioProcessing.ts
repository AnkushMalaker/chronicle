/**
 * AudioProcessingService - Centralized audio processing operations
 * Consolidates audio buffer management, WAV creation, and audio segment extraction
 * used across Inference, InferLive, and Speakers pages
 */

import { createWAVHeader, createWAVBlob, concatenateAudioBuffers, extractAudioSegmentFromBuffers } from '../utils/audioUtils'

export interface AudioBuffer {
  samples: Float32Array
  sampleRate: number
  channels: number
  duration: number
}

export interface AudioSegmentInfo {
  start: number
  end: number
  duration: number
  speaker?: string
  text?: string
  confidence?: number
}

export interface ProcessedAudio {
  file: File | Blob
  filename: string
  buffer: AudioBuffer
  quality?: {
    snr: number
    level: string
  }
}

export interface RecordingState {
  isRecording: boolean
  duration: number
  status: 'idle' | 'starting' | 'recording' | 'stopping' | 'error'
  error?: string
}

export interface UtteranceExtractionResult {
  audioBuffer: Float32Array
  duration: number
  isValid: boolean
  error?: string
}

export class AudioProcessingService {
  private static instance: AudioProcessingService

  // Audio processing constants
  private readonly SUPPORTED_FORMATS = ['audio/wav', 'audio/webm', 'audio/mp4']
  private readonly TARGET_SAMPLE_RATE = 16000
  private readonly BUFFER_DURATION_MS = 256 // Each buffer represents ~256ms
  private readonly MAX_BUFFERS = 750 // 30 seconds at 4096 samples per 250ms

  static getInstance(): AudioProcessingService {
    if (!AudioProcessingService.instance) {
      AudioProcessingService.instance = new AudioProcessingService()
    }
    return AudioProcessingService.instance
  }

  /**
   * Process uploaded audio file for analysis
   */
  async processAudioFile(file: File): Promise<ProcessedAudio> {
    try {
      // Validate file format
      if (!this.isValidAudioFile(file)) {
        throw new Error('Unsupported audio format. Please use WAV files for best compatibility.')
      }

      // Load and decode audio
      const arrayBuffer = await this.loadFileAsArrayBuffer(file)
      const audioContext = this.createAudioContext()
      const audioBuffer = await this.decodeAudioData(audioContext, arrayBuffer)

      // Extract samples (convert to mono if needed)
      const samples = this.extractMonoSamples(audioBuffer)

      // Create processed audio object
      const processed: ProcessedAudio = {
        file,
        filename: file.name,
        buffer: {
          samples,
          sampleRate: audioBuffer.sampleRate,
          channels: 1, // Always mono after processing
          duration: audioBuffer.duration
        }
      }

      // Calculate audio quality metrics
      processed.quality = this.calculateAudioQuality(samples)

      return processed
    } catch (error) {
      throw new Error(`Failed to process audio file: ${error.message}`)
    }
  }

  /**
   * Convert recording blob to processed audio
   */
  async processRecordingBlob(blob: Blob, filename: string = 'Recording'): Promise<ProcessedAudio> {
    try {
      // Convert WebM to WAV if needed
      let processedBlob = blob
      if (blob.type.includes('webm')) {
        processedBlob = await this.convertBlobToWav(blob)
      }

      // Process as file
      const file = new File([processedBlob], `${filename}.wav`, { type: 'audio/wav' })
      return await this.processAudioFile(file)
    } catch (error) {
      throw new Error(`Failed to process recording: ${error.message}`)
    }
  }

  /**
   * Create WAV blob from Float32Array samples
   */
  createWavBlob(samples: Float32Array, sampleRate: number = this.TARGET_SAMPLE_RATE): Blob {
    return createWAVBlob(samples, sampleRate)
  }

  /**
   * Extract utterance audio from buffer array based on timing
   */
  extractUtteranceAudio(
    audioBuffers: Float32Array[],
    utteranceStartTime: number,
    utteranceEndTime: number,
    streamStartTime?: number,
    sampleRate: number = this.TARGET_SAMPLE_RATE
  ): UtteranceExtractionResult {
    try {
      // Validate inputs
      if (!audioBuffers || audioBuffers.length === 0) {
        return {
          audioBuffer: new Float32Array(0),
          duration: 0,
          isValid: false,
          error: 'No audio buffers available'
        }
      }

      if (utteranceStartTime >= utteranceEndTime) {
        return {
          audioBuffer: new Float32Array(0),
          duration: 0,
          isValid: false,
          error: 'Invalid utterance timing'
        }
      }

      // Calculate buffer indices using dynamic buffer duration
      // Assume standard 4096 samples per buffer
      const samplesPerBuffer = audioBuffers.length > 0 ? audioBuffers[0].length : 4096
      const actualBufferDurationMs = (samplesPerBuffer / sampleRate) * 1000

      console.log(`🎵 [AUDIO EXTRACTION] Sample rate: ${sampleRate}Hz, Buffer duration: ${actualBufferDurationMs.toFixed(2)}ms`)
      console.log(`🕐 [TIMING] Utterance: ${utteranceStartTime.toFixed(3)}s - ${utteranceEndTime.toFixed(3)}s`)

      const startBufferIndex = Math.max(0, Math.floor(utteranceStartTime * 1000 / actualBufferDurationMs))
      const endBufferIndex = Math.min(audioBuffers.length, Math.ceil(utteranceEndTime * 1000 / actualBufferDurationMs))

      if (startBufferIndex >= endBufferIndex || startBufferIndex >= audioBuffers.length) {
        return {
          audioBuffer: new Float32Array(0),
          duration: 0,
          isValid: false,
          error: 'Invalid buffer range calculated'
        }
      }

      // Extract utterance buffers
      const utteranceAudioBuffer = extractAudioSegmentFromBuffers(audioBuffers, startBufferIndex, endBufferIndex)

      if (!utteranceAudioBuffer) {
        return {
          audioBuffer: new Float32Array(0),
          duration: 0,
          isValid: false,
          error: 'Failed to extract audio segment'
        }
      }

      console.log(`✅ [EXTRACTION SUCCESS] Extracted ${utteranceAudioBuffer.length} samples (${(utteranceAudioBuffer.length / sampleRate).toFixed(3)}s)`)

      return {
        audioBuffer: utteranceAudioBuffer,
        duration: utteranceAudioBuffer.length / sampleRate,
        isValid: true
      }
    } catch (error) {
      return {
        audioBuffer: new Float32Array(0),
        duration: 0,
        isValid: false,
        error: `Audio extraction failed: ${error.message}`
      }
    }
  }

  /**
   * Manage audio buffer array for live processing
   */
  manageAudioBufferArray(buffers: Float32Array[], newBuffer: Float32Array, sampleRate: number = this.TARGET_SAMPLE_RATE): Float32Array[] {
    const updatedBuffers = [...buffers, new Float32Array(newBuffer)]

    // Calculate dynamic buffer duration based on actual sample rate
    const actualBufferDurationMs = (newBuffer.length / sampleRate) * 1000
    const bufferRetentionMs = 120 * 1000 // Keep 120 seconds of audio for utterance capture
    const maxBuffers = Math.ceil(bufferRetentionMs / actualBufferDurationMs)

    // Keep only last 120 seconds of audio (adjusted for actual sample rate)
    if (updatedBuffers.length > maxBuffers) {
      updatedBuffers.shift()
    }

    return updatedBuffers
  }

  /**
   * Extract audio segment from samples by time
   */
  extractAudioSegment(
    samples: Float32Array,
    startTime: number,
    endTime: number,
    sampleRate: number
  ): Float32Array {
    const startSample = Math.floor(startTime * sampleRate)
    const endSample = Math.floor(endTime * sampleRate)

    const segmentLength = Math.max(0, Math.min(endSample - startSample, samples.length - startSample))
    const segment = new Float32Array(segmentLength)

    for (let i = 0; i < segmentLength; i++) {
      segment[i] = samples[startSample + i] || 0
    }

    return segment
  }

  /**
   * Validate if file is a supported audio format
   */
  isValidAudioFile(file: File): boolean {
    // Check file extension for WAV specifically (most reliable)
    const isWav = file.name.toLowerCase().endsWith('.wav')
    const isSupportedType = this.SUPPORTED_FORMATS.some(format => file.type.includes(format.split('/')[1]))

    return isWav || isSupportedType
  }

  /**
   * Create audio context with cross-browser compatibility
   */
  createAudioContext(options?: AudioContextOptions): AudioContext {
    const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext
    return new AudioContextClass({
      sampleRate: this.TARGET_SAMPLE_RATE,
      ...options
    })
  }

  /**
   * Load file as ArrayBuffer
   */
  private async loadFileAsArrayBuffer(file: File): Promise<ArrayBuffer> {
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
   * Decode audio data with promise wrapper
   */
  private async decodeAudioData(audioContext: AudioContext, arrayBuffer: ArrayBuffer): Promise<AudioBuffer> {
    return new Promise((resolve, reject) => {
      audioContext.decodeAudioData(
        arrayBuffer,
        (audioBuffer) => resolve(audioBuffer),
        (error) => reject(new Error(`Audio decoding failed: ${error}`))
      )
    })
  }

  /**
   * Extract mono samples from AudioBuffer
   */
  private extractMonoSamples(audioBuffer: AudioBuffer): Float32Array {
    if (audioBuffer.numberOfChannels === 1) {
      return new Float32Array(audioBuffer.getChannelData(0))
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
   * Convert blob to WAV format
   */
  private async convertBlobToWav(blob: Blob): Promise<Blob> {
    if (blob.type.includes('wav')) {
      return blob
    }

    const arrayBuffer = await blob.arrayBuffer()
    const audioContext = this.createAudioContext()
    const audioBuffer = await this.decodeAudioData(audioContext, arrayBuffer)
    const samples = this.extractMonoSamples(audioBuffer)

    return this.createWavBlob(samples, audioBuffer.sampleRate)
  }

  /**
   * Calculate audio quality metrics
   */
  private calculateAudioQuality(samples: Float32Array): { snr: number; level: string } {
    if (samples.length === 0) {
      return { snr: 0, level: 'poor' }
    }

    // Calculate RMS for signal power
    let sumSquares = 0
    for (let i = 0; i < samples.length; i++) {
      sumSquares += samples[i] * samples[i]
    }
    const rms = Math.sqrt(sumSquares / samples.length)

    // Estimate noise floor (lowest 10% of values)
    const sorted = Array.from(samples).map(Math.abs).sort((a, b) => a - b)
    const noiseFloorIndex = Math.floor(sorted.length * 0.1)
    const noiseFloor = sorted[noiseFloorIndex] || 0.001

    // SNR in dB
    const snr = 20 * Math.log10(rms / noiseFloor)
    const validSnr = isFinite(snr) ? snr : 0

    // Classify quality level
    let level: string
    if (validSnr >= 30) level = 'excellent'
    else if (validSnr >= 20) level = 'good'
    else if (validSnr >= 15) level = 'fair'
    else level = 'poor'

    return { snr: validSnr, level }
  }
}

// Export singleton instance
export const audioProcessingService = AudioProcessingService.getInstance()
