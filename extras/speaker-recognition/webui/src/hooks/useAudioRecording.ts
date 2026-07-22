/**
 * useAudioRecording Hook - Centralized audio recording functionality
 * Handles microphone access, recording state, and audio processing
 * Used across Inference, InferLive, and other components requiring audio recording
 */

import { useState, useRef, useCallback, useEffect } from 'react'
import { audioProcessingService, ProcessedAudio, RecordingState } from '../services/audioProcessing'

export interface UseAudioRecordingOptions {
  sampleRate?: number
  channels?: number
  bufferSize?: number
  autoProcess?: boolean
  maxDuration?: number // in seconds
  onAudioProcessed?: (audio: ProcessedAudio) => void
  onError?: (error: string) => void
}

export interface UseAudioRecordingReturn {
  // State
  recordingState: RecordingState
  processedAudio: ProcessedAudio | null

  // Controls
  startRecording: () => Promise<void>
  stopRecording: () => Promise<void>
  clearRecording: () => void

  // Processing
  processCurrentRecording: () => Promise<ProcessedAudio | null>

  // Audio stream access (for live processing)
  mediaStream: MediaStream | null
  audioContext: AudioContext | null
}

export const useAudioRecording = (options: UseAudioRecordingOptions = {}): UseAudioRecordingReturn => {
  const {
    sampleRate = 16000,
    channels = 1,
    autoProcess = true,
    maxDuration = 300, // 5 minutes default
    onAudioProcessed,
    onError
  } = options

  // State
  const [recordingState, setRecordingState] = useState<RecordingState>({
    isRecording: false,
    duration: 0,
    status: 'idle'
  })
  const [processedAudio, setProcessedAudio] = useState<ProcessedAudio | null>(null)

  // Refs for recording infrastructure
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const recordingTimerRef = useRef<NodeJS.Timeout | null>(null)
  const startTimeRef = useRef<number>(0)

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopRecording()
      if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
        audioContextRef.current.close()
      }
    }
  }, [])

  // Recording timer
  useEffect(() => {
    if (recordingState.isRecording) {
      recordingTimerRef.current = setInterval(() => {
        const elapsed = (Date.now() - startTimeRef.current) / 1000
        setRecordingState(prev => ({ ...prev, duration: elapsed }))

        // Auto-stop if max duration reached
        if (elapsed >= maxDuration) {
          stopRecording()
        }
      }, 1000)
    } else {
      if (recordingTimerRef.current) {
        clearInterval(recordingTimerRef.current)
        recordingTimerRef.current = null
      }
    }

    return () => {
      if (recordingTimerRef.current) {
        clearInterval(recordingTimerRef.current)
      }
    }
  }, [recordingState.isRecording, maxDuration])

  const validateBrowserSupport = (): { isSupported: boolean; error?: string } => {
    // Check HTTPS requirement
    if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
      return {
        isSupported: false,
        error: 'Microphone access requires HTTPS. Please use HTTPS or localhost.'
      }
    }

    // Check MediaRecorder support
    if (!window.MediaRecorder) {
      return {
        isSupported: false,
        error: 'MediaRecorder not supported in this browser.'
      }
    }

    // Check getUserMedia support
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return {
        isSupported: false,
        error: 'Microphone access not supported in this browser.'
      }
    }

    return { isSupported: true }
  }

  const getMicrophoneStream = async (): Promise<MediaStream> => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate,
          channelCount: channels,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      })
      return stream
    } catch (error: any) {
      let errorMessage = 'Failed to access microphone. '

      if (error.name === 'NotAllowedError') {
        errorMessage += 'Please allow microphone access and try again.'
      } else if (error.name === 'NotFoundError') {
        errorMessage += 'No microphone found. Please check your device.'
      } else if (error.name === 'NotSupportedError') {
        errorMessage += 'Recording not supported in this browser.'
      } else {
        errorMessage += 'Please check permissions and try again.'
      }

      throw new Error(errorMessage)
    }
  }

  const setupMediaRecorder = (stream: MediaStream): MediaRecorder => {
    // Try different MIME types for best compatibility
    let mimeType = 'audio/wav'
    if (!MediaRecorder.isTypeSupported(mimeType)) {
      mimeType = 'audio/webm'
      if (!MediaRecorder.isTypeSupported(mimeType)) {
        mimeType = '' // Let browser choose
      }
    }

    const mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        audioChunksRef.current.push(event.data)
      }
    }

    mediaRecorder.onstop = async () => {
      try {
        const blob = new Blob(audioChunksRef.current, {
          type: mimeType || 'audio/webm'
        })

        // Auto-process if enabled
        if (autoProcess) {
          await processRecordingBlob(blob)
        }
      } catch (error) {
        const errorMsg = `Failed to process recording: ${error instanceof Error ? error.message : String(error)}`
        setRecordingState(prev => ({ ...prev, status: 'error', error: errorMsg }))
        onError?.(errorMsg)
      }
    }

    mediaRecorder.onerror = (_event) => {
      const errorMsg = 'Recording failed. Please try again.'
      setRecordingState(prev => ({ ...prev, status: 'error', error: errorMsg }))
      onError?.(errorMsg)
    }

    return mediaRecorder
  }

  const processRecordingBlob = async (blob: Blob): Promise<ProcessedAudio | null> => {
    try {
      const timestamp = new Date().toLocaleString()
      const processed = await audioProcessingService.processRecordingBlob(
        blob,
        `Recording ${timestamp}`
      )

      setProcessedAudio(processed)
      onAudioProcessed?.(processed)

      return processed
    } catch (error) {
      const errorMsg = `Failed to process recording: ${error instanceof Error ? error.message : String(error)}`
      setRecordingState(prev => ({ ...prev, status: 'error', error: errorMsg }))
      onError?.(errorMsg)
      return null
    }
  }

  const startRecording = useCallback(async (): Promise<void> => {
    try {
      // Validate browser support
      const validation = validateBrowserSupport()
      if (!validation.isSupported) {
        throw new Error(validation.error)
      }

      setRecordingState(prev => ({ ...prev, status: 'starting' }))

      // Get microphone stream
      const stream = await getMicrophoneStream()
      mediaStreamRef.current = stream

      // Create audio context for potential live processing
      audioContextRef.current = audioProcessingService.createAudioContext({ sampleRate })

      // Setup MediaRecorder
      audioChunksRef.current = []
      mediaRecorderRef.current = setupMediaRecorder(stream)

      // Start recording
      startTimeRef.current = Date.now()
      mediaRecorderRef.current.start(250) // Collect data every 250ms

      setRecordingState({
        isRecording: true,
        duration: 0,
        status: 'recording'
      })

    } catch (error: any) {
      const errorMsg = error.message || 'Failed to start recording'
      setRecordingState({
        isRecording: false,
        duration: 0,
        status: 'error',
        error: errorMsg
      })
      onError?.(errorMsg)
    }
  }, [sampleRate, channels, autoProcess, onError, onAudioProcessed])

  const stopRecording = useCallback(async (): Promise<void> => {
    if (!recordingState.isRecording || !mediaRecorderRef.current) {
      return
    }

    try {
      setRecordingState(prev => ({ ...prev, status: 'stopping' }))

      // Stop MediaRecorder
      if (mediaRecorderRef.current.state === 'recording') {
        mediaRecorderRef.current.stop()
      }

      // Stop media stream
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach(track => track.stop())
        mediaStreamRef.current = null
      }

      // Clear timer
      if (recordingTimerRef.current) {
        clearInterval(recordingTimerRef.current)
        recordingTimerRef.current = null
      }

      setRecordingState(prev => ({
        ...prev,
        isRecording: false,
        status: 'idle'
      }))

    } catch (error: any) {
      const errorMsg = `Failed to stop recording: ${error.message}`
      setRecordingState(prev => ({
        ...prev,
        isRecording: false,
        status: 'error',
        error: errorMsg
      }))
      onError?.(errorMsg)
    }
  }, [recordingState.isRecording, onError])

  const clearRecording = useCallback((): void => {
    setProcessedAudio(null)
    setRecordingState({
      isRecording: false,
      duration: 0,
      status: 'idle'
    })
    audioChunksRef.current = []
  }, [])

  const processCurrentRecording = useCallback(async (): Promise<ProcessedAudio | null> => {
    if (audioChunksRef.current.length === 0) {
      return null
    }

    const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
    return await processRecordingBlob(blob)
  }, [])

  return {
    // State
    recordingState,
    processedAudio,

    // Controls
    startRecording,
    stopRecording,
    clearRecording,

    // Processing
    processCurrentRecording,

    // Access to streams (for live processing)
    mediaStream: mediaStreamRef.current,
    audioContext: audioContextRef.current
  }
}
