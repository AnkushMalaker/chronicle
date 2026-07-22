/**
 * useSpeakerIdentification Hook - Enhanced speaker identification state management
 * Centralizes speaker identification processing state and operations
 * Supports all processing modes: diarization, deepgram, hybrid, plain
 */

import { useState, useCallback, useRef } from 'react'
import { useUser } from '../contexts/UserContext'
import { apiService } from '../services/api'
import { createWAVBlob } from '../utils/audioUtils'
import { speakerLogger } from '../utils/logger'
import {
  speakerIdentificationService,
  ProcessingMode,
  ProcessingOptions,
  ProcessingResult
} from '../services/speakerIdentification'
import { ProcessedAudio } from '../services/audioProcessing'

export interface IdentifyResult {
  found: boolean
  speaker_id: string | null
  speaker_name: string | null
  confidence: number
  status: string
  similarity_threshold: number
  duration: number
}

export interface UseSpeakerIdentificationOptions {
  defaultMode?: ProcessingMode
  defaultConfidenceThreshold?: number
  onProcessingComplete?: (result: ProcessingResult) => void
  onError?: (error: string) => void
}

export interface UseSpeakerIdentificationReturn {
  // Legacy method for backward compatibility
  identifyUtteranceSpeaker: (audioBuffer: Float32Array, sampleRate: number) => Promise<IdentifyResult>

  // New enhanced functionality
  isProcessing: boolean
  currentMode: ProcessingMode
  confidenceThreshold: number
  results: ProcessingResult[]
  selectedResult: ProcessingResult | null
  processingProgress: string | null

  // Controls
  setProcessingMode: (mode: ProcessingMode) => void
  setConfidenceThreshold: (threshold: number) => void
  processAudio: (audio: ProcessedAudio, modeOrOptions?: ProcessingMode | Partial<ProcessingOptions>) => Promise<ProcessingResult | null>
  selectResult: (result: ProcessingResult | null) => void
  clearResults: () => void
  exportResult: (result: ProcessingResult) => void

  // Available modes
  availableModes: Array<{ mode: ProcessingMode; name: string; description: string }>
}

export function useSpeakerIdentification(
  options: UseSpeakerIdentificationOptions = {}
): UseSpeakerIdentificationReturn {
  const { user } = useUser()
  const {
    defaultMode = 'speaker-identification',
    defaultConfidenceThreshold = 0.15,
    onProcessingComplete,
    onError
  } = options

  // State
  const [isProcessing, setIsProcessing] = useState(false)
  const [currentMode, setCurrentMode] = useState<ProcessingMode>(defaultMode)
  const [confidenceThreshold, setConfidenceThreshold] = useState(defaultConfidenceThreshold)
  const [results, setResults] = useState<ProcessingResult[]>([])
  const [selectedResult, setSelectedResult] = useState<ProcessingResult | null>(null)
  const [processingProgress, setProcessingProgress] = useState<string | null>(null)

  // Refs for tracking operations
  const processingAbortController = useRef<AbortController | null>(null)

  // Get available processing modes
  const availableModes = speakerIdentificationService.getAvailableModes()

  // Legacy method for backward compatibility
  const identifyUtteranceSpeaker = useCallback(async (
    audioBuffer: Float32Array,
    sampleRate: number
  ): Promise<IdentifyResult> => {
    try {
      speakerLogger.info('Starting utterance speaker identification')

      // Create WAV blob
      const wavBlob = createWAVBlob(audioBuffer, sampleRate)

      // Call the simple identify-utterance endpoint
      const formData = new FormData()
      formData.append('file', wavBlob, 'utterance.wav')
      formData.append('similarity_threshold', confidenceThreshold.toString())
      if (user?.id) {
        formData.append('user_id', user.id.toString())
      }

      const response = await apiService.post('/identify', formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
        timeout: 15000, // 15 seconds for utterance identification
      })

      speakerLogger.info('API Response:', response.data)

      return response.data as IdentifyResult

    } catch (error) {
      speakerLogger.error('Error during utterance identification:', error)
      return {
        found: false,
        speaker_id: null,
        speaker_name: null,
        confidence: 0,
        status: 'error',
        similarity_threshold: confidenceThreshold,
        duration: 0
      }
    }
  }, [user, confidenceThreshold])

  // New enhanced functionality
  const setProcessingMode = useCallback((mode: ProcessingMode) => {
    setCurrentMode(mode)
  }, [])

  const setThreshold = useCallback((threshold: number) => {
    setConfidenceThreshold(Math.max(0, Math.min(1, threshold)))
  }, [])

  const processAudio = useCallback(async (
    audio: ProcessedAudio,
    modeOrOptions?: ProcessingMode | Partial<ProcessingOptions>
  ): Promise<ProcessingResult | null> => {
    // Handle both old signature (mode) and new signature (options)
    let processingOptions: ProcessingOptions
    if (typeof modeOrOptions === 'string') {
      // Legacy mode parameter
      processingOptions = {
        mode: modeOrOptions,
        userId: user?.id,
        confidenceThreshold,
        minDuration: 1.0,
        identifyOnlyEnrolled: false,
        enhanceSpeakers: true
      }
    } else {
      // New options object
      const options = modeOrOptions || {}
      processingOptions = {
        mode: options.mode || currentMode,
        userId: user?.id,
        confidenceThreshold,
        minDuration: 1.0,
        identifyOnlyEnrolled: false,
        enhanceSpeakers: true,
        ...options
      }
    }

    const processingMode = processingOptions.mode

    try {
      setIsProcessing(true)
      setProcessingProgress(`Starting ${processingMode} processing...`)

      // Create abort controller for this operation
      processingAbortController.current = new AbortController()

      // Update progress based on mode
      const progressMessages: Record<string, string> = {
        'diarization-only': 'Performing diarization...',
        'speaker-identification': 'Analyzing speakers...',
        'deepgram-enhanced': 'Transcribing with Deepgram...',
        'deepgram-transcript-internal-speakers': 'Processing hybrid mode...',
        'diarize-identify-match': 'Matching transcript to speakers...'
      }
      setProcessingProgress(progressMessages[processingMode] || 'Processing audio...')

      // Ensure we have a valid file
      if (!audio.file) {
        throw new Error('ProcessedAudio object is missing the file property')
      }

      // Process audio with all options
      const result = await speakerIdentificationService.processAudio(
        audio.file,
        processingOptions
      )

      // Add to results
      setResults((prev: ProcessingResult[]) => [result, ...prev])
      setSelectedResult(result)

      // Call completion callback
      onProcessingComplete?.(result)

      setProcessingProgress(null)
      return result

    } catch (error: any) {
      const errorMsg = `${processingMode} processing failed: ${error.message}`
      setProcessingProgress(null)
      onError?.(errorMsg)

      // Create failed result for tracking
      const failedResult: ProcessingResult = {
        id: Math.random().toString(36),
        filename: audio.filename,
        duration: audio.buffer.duration,
        status: 'failed',
        created_at: new Date().toISOString(),
        mode: processingMode,
        speakers: [],
        confidence_summary: {
          total_segments: 0,
          high_confidence: 0,
          medium_confidence: 0,
          low_confidence: 0
        },
        error: error.message
      }

      setResults((prev: ProcessingResult[]) => [failedResult, ...prev])
      return null

    } finally {
      setIsProcessing(false)
      processingAbortController.current = null
    }
  }, [currentMode, confidenceThreshold, user?.id, onProcessingComplete, onError])

  const selectResult = useCallback((result: ProcessingResult | null) => {
    setSelectedResult(result)
  }, [])

  const clearResults = useCallback(() => {
    setResults([])
    setSelectedResult(null)
  }, [])

  const exportResult = useCallback((result: ProcessingResult) => {
    try {
      const exportData = {
        filename: result.filename,
        duration: result.duration,
        mode: result.mode,
        created_at: result.created_at,
        processing_time: result.processing_time,
        confidence_summary: result.confidence_summary,
        speakers: result.speakers.map(segment => ({
          start: segment.start,
          end: segment.end,
          duration: segment.end - segment.start,
          speaker: segment.speaker_name,
          speaker_id: segment.speaker_id,
          confidence: segment.confidence,
          text: segment.text,
          identified_speaker_id: segment.identified_speaker_id,
          identified_speaker_name: segment.identified_speaker_name,
          speaker_identification_confidence: segment.speaker_identification_confidence,
          speaker_status: segment.speaker_status
        })),
        deepgram_response: result.deepgram_response
      }

      const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `speaker_${result.mode}_${result.filename.split('.')[0]}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)

    } catch (error: any) {
      onError?.(`Export failed: ${error.message}`)
    }
  }, [onError])

  return {
    // Legacy method for backward compatibility
    identifyUtteranceSpeaker,

    // New enhanced functionality
    isProcessing,
    currentMode,
    confidenceThreshold,
    results,
    selectedResult,
    processingProgress,

    // Controls
    setProcessingMode,
    setConfidenceThreshold: setThreshold,
    processAudio,
    selectResult,
    clearResults,
    exportResult,

    // Available modes
    availableModes
  }
}
