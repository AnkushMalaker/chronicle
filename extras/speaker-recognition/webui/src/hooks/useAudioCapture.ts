/**
 * Custom hook for managing audio capture using Web Audio API
 */

import { useRef, useCallback } from 'react'
import { DeepgramStreaming } from '../services/deepgram'
import { audioLogger } from '../utils/logger'
import { validateAudioConfig } from '../utils/audioUtils'

export interface AudioCaptureConfig {
  sampleRate: number
  channelCount: number
  bufferSize: number
  maxBufferLength: number // Maximum number of buffers to keep in memory
}

export interface UseAudioCaptureReturn {
  audioBufferRef: React.MutableRefObject<Float32Array[]>
  mediaStreamRef: React.MutableRefObject<MediaStream | null>
  startAudioCapture: (deepgramRef: React.MutableRefObject<DeepgramStreaming | null>) => Promise<void>
  stopAudioCapture: () => void
  isCapturing: boolean
}

const DEFAULT_CONFIG: AudioCaptureConfig = {
  sampleRate: 16000,
  channelCount: 1,
  bufferSize: 4096,
  maxBufferLength: 750 // 30 seconds at 4096 samples per 250ms
}

export function useAudioCapture(config: Partial<AudioCaptureConfig> = {}): UseAudioCaptureReturn {
  const audioConfig = { ...DEFAULT_CONFIG, ...config }

  // Refs for audio management
  const audioBufferRef = useRef<Float32Array[]>([])
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const isCapturingRef = useRef(false)

  // Validate configuration
  const validation = validateAudioConfig(audioConfig)
  if (!validation.isValid) {
    console.warn('Invalid audio configuration:', validation.errors)
  }

  const startAudioCapture = useCallback(async (
    deepgramRef: React.MutableRefObject<DeepgramStreaming | null>
  ): Promise<void> => {
    if (isCapturingRef.current) {
      audioLogger.warn('Audio capture already in progress')
      return
    }

    try {
      audioLogger.info('Starting audio capture...')

      // Request microphone access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: audioConfig.sampleRate,
          channelCount: audioConfig.channelCount,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      })

      mediaStreamRef.current = stream
      isCapturingRef.current = true

      // Use Web Audio API to process audio directly to linear16 format
      const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)({
        sampleRate: audioConfig.sampleRate
      })

      const source = audioContext.createMediaStreamSource(stream)
      const processor = audioContext.createScriptProcessor(audioConfig.bufferSize, 1, 1)

      audioLogger.info(`Using Web Audio API for linear16 processing at ${audioConfig.sampleRate}Hz`)

      processor.onaudioprocess = (event) => {
        if (deepgramRef.current && deepgramRef.current.getConnectionStatus() === 'connected') {
          const inputBuffer = event.inputBuffer
          const inputData = inputBuffer.getChannelData(0)

          // Buffer audio for speaker identification
          const audioCopy = new Float32Array(inputData)
          audioBufferRef.current.push(audioCopy)

          // Keep only the last N buffers (rolling window)
          if (audioBufferRef.current.length > audioConfig.maxBufferLength) {
            audioBufferRef.current.shift()
          }

          // Convert Float32Array to Int16 (linear16) for Deepgram
          const int16Buffer = new Int16Array(inputData.length)
          for (let i = 0; i < inputData.length; i++) {
            const sample = Math.max(-1, Math.min(1, inputData[i]))
            int16Buffer[i] = sample * 0x7FFF
          }

          // Send to Deepgram (reduced logging to avoid spam)
          if (Math.random() < 0.01) {
            audioLogger.debug('Sending audio data...')
          }
          deepgramRef.current.sendAudio(int16Buffer.buffer)
        }
      }

      source.connect(processor)
      processor.connect(audioContext.destination)

      // Store references for cleanup
      ;(mediaStreamRef.current as any).audioContext = audioContext
      ;(mediaStreamRef.current as any).processor = processor

      audioLogger.info('Web Audio API processing started')

    } catch (error) {
      isCapturingRef.current = false
      audioLogger.error('Failed to start audio capture:', error)

      let errorMessage = 'Failed to access microphone. '
      if (error instanceof Error) {
        if (error.name === 'NotAllowedError') {
          errorMessage += 'Please allow microphone access and try again.'
        } else if (error.name === 'NotFoundError') {
          errorMessage += 'No microphone found. Please check your device.'
        } else {
          errorMessage += error.message
        }
      }
      throw new Error(errorMessage)
    }
  }, [audioConfig])

  const stopAudioCapture = useCallback(() => {
    audioLogger.info('Stopping audio capture...')

    // Stop Web Audio API components
    if (mediaStreamRef.current) {
      const stream = mediaStreamRef.current as any

      // Stop audio context and processor
      if (stream.audioContext) {
        stream.audioContext.close()
      }
      if (stream.processor) {
        stream.processor.disconnect()
      }

      // Stop media stream tracks
      mediaStreamRef.current.getTracks().forEach(track => track.stop())
      mediaStreamRef.current = null
    }

    // Clear audio buffer
    audioBufferRef.current = []
    isCapturingRef.current = false

    audioLogger.info('Audio capture stopped')
  }, [])

  return {
    audioBufferRef,
    mediaStreamRef,
    startAudioCapture,
    stopAudioCapture,
    isCapturing: isCapturingRef.current
  }
}
