/**
 * useDeepgramIntegration Hook - Deepgram functionality management
 * Centralizes Deepgram streaming and transcription for live processing
 * Integrates with speaker identification for complete audio processing
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import {
  DeepgramStreaming,
  StreamingConfig,
  StreamingTranscript,
  SpeechStartedEvent,
  UtteranceEndEvent
} from '../services/deepgram'
import { audioProcessingService } from '../services/audioProcessing'
import { speakerIdentificationService, IdentifyResult } from '../services/speakerIdentification'
import { useDeepgramSession } from './useDeepgramSession'

export interface UseDeepgramIntegrationOptions {
  apiKey?: string
  userId?: number
  confidenceThreshold?: number
  enableSpeakerIdentification?: boolean
  utteranceEndMs?: number
  endpointingMs?: number
  onTransript?: (transcript: StreamingTranscript) => void
  onSpeechStarted?: (event: SpeechStartedEvent) => void
  onUtteranceEnd?: (event: UtteranceEndEvent) => void
  onSpeakerIdentified?: (result: IdentifyResult) => void
  onError?: (error: string) => void
}

export interface TranscriptSegment {
  id: string
  timestamp: number
  speaker: number
  text: string
  confidence: number
  isInterim: boolean
  speakerParts?: Array<{
    speaker: string
    text: string
    confidence: number
  }>
}

export interface UseDeepgramIntegrationReturn {
  // Connection state
  isConnected: boolean
  connectionStatus: 'disconnected' | 'connecting' | 'connected' | 'error'

  // Streaming state
  isStreaming: boolean
  transcriptSegments: TranscriptSegment[]
  audioBuffers: Float32Array[]

  // API key management
  apiKey: string | null
  apiKeySource: 'server' | 'manual' | 'loading'
  setApiKey: (key: string) => void

  // Controls
  connect: () => Promise<void>
  disconnect: () => void
  startStreaming: () => Promise<void>
  stopStreaming: () => void
  sendAudio: (audioData: ArrayBuffer, sampleRate?: number) => void
  clearTranscripts: () => void

  // Sample rate management
  setActualSampleRate: (sampleRate: number) => void
  actualSampleRate: number

  // Settings
  confidenceThreshold: number
  setConfidenceThreshold: (threshold: number) => void
  enableSpeakerIdentification: boolean
  setEnableSpeakerIdentification: (enabled: boolean) => void
  utteranceEndMs: number
  setUtteranceEndMs: (ms: number) => void
  endpointingMs: number
  setEndpointingMs: (ms: number) => void

  // Statistics
  stats: {
    totalWords: number
    averageConfidence: number
    identifiedSpeakers: Set<string>
    sessionDuration: number
  }
}

export const useDeepgramIntegration = (
  options: UseDeepgramIntegrationOptions = {}
): UseDeepgramIntegrationReturn => {
  const {
    apiKey: initialApiKey,
    userId,
    confidenceThreshold: initialConfidence = 0.15,
    enableSpeakerIdentification: initialEnableId = true,
    utteranceEndMs: initialUtteranceEndMs = 1000,
    endpointingMs: initialEndpointingMs = 300,
    onTransript,
    onSpeechStarted,
    onUtteranceEnd,
    onSpeakerIdentified,
    onError
  } = options

  // Use Deepgram session for API key management
  const deepgramSession = useDeepgramSession()

  // Connection state
  const [isConnected, setIsConnected] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected')

  // Streaming state
  const [isStreaming, setIsStreaming] = useState(false)
  const [transcriptSegments, setTranscriptSegments] = useState<TranscriptSegment[]>([])
  const [audioBuffers, setAudioBuffers] = useState<Float32Array[]>([])

  // API key management - prioritize manual key over session key
  const apiKey = initialApiKey || deepgramSession.deepgramApiKey || null
  const apiKeySource = initialApiKey ? 'manual' : deepgramSession.apiKeySource

  // Settings
  const [confidenceThreshold, setConfidenceThreshold] = useState(initialConfidence)
  const [enableSpeakerIdentification, setEnableSpeakerIdentification] = useState(initialEnableId)
  const [utteranceEndMs, setUtteranceEndMs] = useState(initialUtteranceEndMs)
  const [endpointingMs, setEndpointingMs] = useState(initialEndpointingMs)

  // Sample rate management
  const [actualSampleRate, setActualSampleRate] = useState<number>(16000)

  // Statistics
  const [stats, setStats] = useState({
    totalWords: 0,
    averageConfidence: 0,
    identifiedSpeakers: new Set<string>(),
    sessionDuration: 0
  })

  // Refs
  const deepgramRef = useRef<DeepgramStreaming | null>(null)
  const segmentIdRef = useRef(0)
  const sessionStartRef = useRef<number>(0)
  const streamStartTimeRef = useRef<number | null>(null)
  const utteranceTranscriptsRef = useRef<string[]>([])
  const currentUtteranceSegmentIds = useRef<string[]>([])
  const utteranceStartTimeRef = useRef<number | null>(null)
  const processingUtteranceRef = useRef(false)
  const audioBuffersRef = useRef<Float32Array[]>([]) // Store audio buffers in ref to avoid stale closure

  // Session duration timer
  useEffect(() => {
    if (!isStreaming) return

    const interval = setInterval(() => {
      setStats(prev => ({
        ...prev,
        sessionDuration: Date.now() - sessionStartRef.current
      }))
    }, 1000)

    return () => clearInterval(interval)
  }, [isStreaming])

  // Reset utterance refs helper
  const resetUtteranceRefs = useCallback(() => {
    utteranceTranscriptsRef.current = []
    currentUtteranceSegmentIds.current = []
    utteranceStartTimeRef.current = null
    processingUtteranceRef.current = false
  }, [])

  // Speaker identification for utterances
  const identifyUtteranceSpeaker = useCallback(async (
    _utteranceBuffer: Float32Array,
    utteranceStartTime: number,
    utteranceEndTime: number
  ): Promise<IdentifyResult> => {
    try {
      // Extract the exact utterance timing from buffered audio (use ref to avoid stale closure)
      const extraction = audioProcessingService.extractUtteranceAudio(
        audioBuffersRef.current,
        utteranceStartTime,
        utteranceEndTime,
        streamStartTimeRef.current || 0,
        actualSampleRate
      )

      if (!extraction.isValid) {
        return {
          found: false,
          speaker_id: null,
          speaker_name: null,
          confidence: 0,
          status: 'extraction_failed',
          similarity_threshold: confidenceThreshold,
          duration: 0
        }
      }

      // Create WAV blob for identification
      const wavBlob = audioProcessingService.createWavBlob(extraction.audioBuffer)

      // Use speaker identification service
      const result = await speakerIdentificationService.identifyUtterance(wavBlob, {
        userId,
        confidenceThreshold
      })

      return result || {
        found: false,
        speaker_id: null,
        speaker_name: null,
        confidence: 0,
        status: 'identification_failed',
        similarity_threshold: confidenceThreshold,
        duration: extraction.duration
      }
    } catch (error) {
      console.error('Utterance identification error:', error)
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
  }, [confidenceThreshold, userId, actualSampleRate]) // Removed audioBuffers since we use ref

  // Connect to Deepgram
  const connect = useCallback(async (): Promise<void> => {
    if (!apiKey || isConnected) return

    try {
      const config: StreamingConfig = {
        apiKey,
        model: 'nova-3',
        language: 'multi',
        encoding: 'linear16',
        sample_rate: actualSampleRate,
        interim_results: true,
        vad_events: true,
        utterance_end_ms: utteranceEndMs,
        endpointing: endpointingMs
      }

      deepgramRef.current = new DeepgramStreaming(config)

      // Set up event handlers
      deepgramRef.current.onStatus(setConnectionStatus)
      deepgramRef.current.onError((error) => {
        onError?.(`Deepgram error: ${error.message}`)
        setConnectionStatus('error')
      })

      deepgramRef.current.onTranscript(async (transcript: StreamingTranscript) => {
        if (!transcript.transcript.trim()) return

        const segmentId = `segment_${segmentIdRef.current++}`
        const timestamp = Date.now()

        const segment: TranscriptSegment = {
          id: segmentId,
          timestamp,
          speaker: transcript.words[0]?.speaker ?? 0,
          text: transcript.transcript,
          confidence: transcript.confidence,
          isInterim: !transcript.is_final,
          speakerParts: [{
            speaker: 'N/A',
            text: transcript.transcript,
            confidence: 0.0
          }]
        }

        // Collect for utterance processing
        if (!segment.isInterim) {
          utteranceTranscriptsRef.current.push(transcript.transcript)
          currentUtteranceSegmentIds.current.push(segment.id)

          if (utteranceTranscriptsRef.current.length === 1 && transcript.words.length > 0) {
            utteranceStartTimeRef.current = transcript.words[0].start
          }
        }

        // Update segments
        setTranscriptSegments(prev => {
          if (segment.isInterim) {
            const withoutLastInterim = prev.filter(s => !s.isInterim)
            return [...withoutLastInterim, segment]
          } else {
            const withoutInterim = prev.filter(s => !s.isInterim)
            return [...withoutInterim, segment]
          }
        })

        // Update stats for final transcripts
        if (transcript.is_final) {
          setStats(prev => {
            const wordCount = transcript.words.length
            const totalWords = prev.totalWords + wordCount
            const avgConfidence = totalWords > 0 ?
              (prev.averageConfidence * prev.totalWords + transcript.confidence * wordCount) / totalWords :
              transcript.confidence

            return {
              ...prev,
              totalWords,
              averageConfidence: avgConfidence
            }
          })
        }

        // Call external callback
        onTransript?.(transcript)
      })

      deepgramRef.current.onSpeechStarted((event: SpeechStartedEvent) => {
        onSpeechStarted?.(event)
      })

      deepgramRef.current.onUtteranceEnd(async (event: UtteranceEndEvent) => {
        if (!enableSpeakerIdentification) return

        if (processingUtteranceRef.current) return
        processingUtteranceRef.current = true

        try {
          console.log(`🎤 [UTTERANCE END] Processing utterance end event`)
          console.log(`📝 [UTTERANCE] Transcript segments: ${utteranceTranscriptsRef.current.length}`)
          console.log(`🔗 [UTTERANCE] Segment IDs: ${currentUtteranceSegmentIds.current.length}`)

          if (utteranceTranscriptsRef.current.length === 0 || currentUtteranceSegmentIds.current.length === 0) {
            console.log(`⚠️ [UTTERANCE] No transcripts or segment IDs, skipping`)
            return
          }

          const segmentIdsToUpdate = [...currentUtteranceSegmentIds.current]
          const utteranceStartTime = utteranceStartTimeRef.current
          const utteranceEndTime = event.last_word_end

          console.log(`⏰ [UTTERANCE TIMING] Start: ${utteranceStartTime}, End: ${utteranceEndTime}`)
          console.log(`🔊 [AUDIO BUFFERS] Available buffers: ${audioBuffersRef.current.length}`)

          if (!utteranceStartTime) {
            console.log(`⚠️ [UTTERANCE] No start time, skipping`)
            return
          }

          // Identify speaker for this utterance
          console.log(`🔍 [SPEAKER ID] Starting identification for utterance`)
          const identification = await identifyUtteranceSpeaker(
            new Float32Array(0), // Will be extracted from audioBuffers
            utteranceStartTime,
            utteranceEndTime
          )

          console.log(`🎯 [SPEAKER ID] Result:`, {
            found: identification.found,
            speaker_name: identification.speaker_name,
            confidence: identification.confidence,
            status: identification.status
          })

          // Update transcript segments with speaker information
          setTranscriptSegments(prev => {
            const updatedSegments = [...prev]

            segmentIdsToUpdate.forEach((segmentId) => {
              const segmentIndex = updatedSegments.findIndex(seg => seg.id === segmentId)

              if (segmentIndex >= 0) {
                const speakerName = identification.found ?
                  identification.speaker_name : 'Unknown'

                updatedSegments[segmentIndex] = {
                  ...updatedSegments[segmentIndex],
                  speakerParts: [{
                    speaker: speakerName || 'Unknown',
                    text: updatedSegments[segmentIndex].text,
                    confidence: identification.confidence
                  }]
                }
              }
            })

            return updatedSegments
          })

          // Update identified speakers stats
          if (identification.found && identification.speaker_name) {
            setStats(prev => ({
              ...prev,
              identifiedSpeakers: new Set([...prev.identifiedSpeakers, identification.speaker_name!])
            }))
          }

          // Call external callback
          onSpeakerIdentified?.(identification)

        } catch (error) {
          console.error('Utterance end processing error:', error)
        } finally {
          resetUtteranceRefs()
        }

        // Call external callback
        onUtteranceEnd?.(event)
      })

      // Connect
      await deepgramRef.current.connect()
      setIsConnected(true)

    } catch (error: any) {
      onError?.(`Failed to connect to Deepgram: ${error.message}`)
      setConnectionStatus('error')
    }
  }, [apiKey, isConnected, actualSampleRate, enableSpeakerIdentification, identifyUtteranceSpeaker, onTransript, onSpeechStarted, onUtteranceEnd, onSpeakerIdentified, onError, resetUtteranceRefs])

  // Disconnect from Deepgram
  const disconnect = useCallback(() => {
    if (deepgramRef.current) {
      deepgramRef.current.disconnect()
      deepgramRef.current = null
    }
    setIsConnected(false)
    setIsStreaming(false)
    setConnectionStatus('disconnected')
  }, [])

  // Start streaming
  const startStreaming = useCallback(async (): Promise<void> => {
    if (!isConnected) {
      await connect()
    }

    sessionStartRef.current = Date.now()
    streamStartTimeRef.current = Date.now() / 1000
    setIsStreaming(true)
    setAudioBuffers([])
    audioBuffersRef.current = [] // Clear ref as well
    setTranscriptSegments([])
    resetUtteranceRefs()
  }, [isConnected, connect, resetUtteranceRefs])

  // Stop streaming
  const stopStreaming = useCallback(() => {
    if (deepgramRef.current) {
      deepgramRef.current.finishStream()
    }
    setIsStreaming(false)
  }, [])

  // Send audio data
  const sendAudio = useCallback((audioData: ArrayBuffer, _sampleRate?: number) => {
    if (deepgramRef.current && isConnected && isStreaming) {
      deepgramRef.current.sendAudio(audioData)

      // Also buffer the audio for speaker identification
      const int16Array = new Int16Array(audioData)
      const float32Array = new Float32Array(int16Array.length)

      // Convert Int16 to Float32
      for (let i = 0; i < int16Array.length; i++) {
        float32Array[i] = int16Array[i] / 0x7FFF
      }

      setAudioBuffers(prev => {
        const updated = audioProcessingService.manageAudioBufferArray(prev, float32Array, actualSampleRate)
        // Update ref as well to avoid stale closure in callbacks
        audioBuffersRef.current = updated
        // Log buffer status periodically
        if (prev.length === 0 && updated.length > 0) {
          console.log(`🎵 [AUDIO BUFFER] First buffer added, size: ${float32Array.length} samples`)
        } else if (updated.length % 10 === 0) {
          console.log(`🎵 [AUDIO BUFFER] Total buffers: ${updated.length}`)
        }
        return updated
      })
    } else {
      console.warn(`⚠️ [AUDIO] Cannot send audio - Connected: ${isConnected}, Streaming: ${isStreaming}`)
    }
  }, [isConnected, isStreaming, actualSampleRate])

  // Clear transcripts
  const clearTranscripts = useCallback(() => {
    setTranscriptSegments([])
    setStats({
      totalWords: 0,
      averageConfidence: 0,
      identifiedSpeakers: new Set(),
      sessionDuration: 0
    })
    resetUtteranceRefs()
  }, [resetUtteranceRefs])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      disconnect()
    }
  }, [disconnect])

  return {
    // Connection state
    isConnected,
    connectionStatus,

    // Streaming state
    isStreaming,
    transcriptSegments,
    audioBuffers,

    // API key management
    apiKey,
    apiKeySource,
    setApiKey: deepgramSession.setDeepgramApiKey,

    // Controls
    connect,
    disconnect,
    startStreaming,
    stopStreaming,
    sendAudio,
    clearTranscripts,

    // Sample rate management
    setActualSampleRate,
    actualSampleRate,

    // Settings
    confidenceThreshold,
    setConfidenceThreshold,
    enableSpeakerIdentification,
    setEnableSpeakerIdentification,
    utteranceEndMs,
    setUtteranceEndMs,
    endpointingMs,
    setEndpointingMs,

    // Statistics
    stats
  }
}
