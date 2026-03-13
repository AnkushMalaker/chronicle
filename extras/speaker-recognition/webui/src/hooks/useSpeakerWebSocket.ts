/**
 * Hook for using the Speaker WebSocket service
 * Provides a simplified interface for real-time transcription with speaker detection
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import {
  SpeakerWebSocketService,
  UtteranceBoundaryEvent,
  SpeakerWebSocketOptions
} from '../services/speakerWebSocket'

export interface TranscriptSegment {
  id: string
  timestamp: number
  text: string
  speaker_name: string | null
  speaker_id: string | null
  confidence: number
  duration: number
  status: 'identified' | 'unknown' | 'error' | 'interim'
  audio_segment: {
    start: number
    end: number
    duration: number
  }
}

export interface UseSpeakerWebSocketReturn {
  // Connection state
  isConnected: boolean
  connectionStatus: 'connecting' | 'connected' | 'disconnected' | 'error'

  // Streaming state
  isStreaming: boolean
  transcriptSegments: TranscriptSegment[]

  // Controls
  connect: () => Promise<void>
  disconnect: () => void
  startStreaming: () => void
  stopStreaming: () => void
  sendAudio: (audioData: ArrayBuffer | Uint8Array) => boolean
  clearTranscripts: () => void

  // Settings
  updateSettings: (settings: Partial<SpeakerWebSocketOptions>) => void

  // Statistics
  stats: {
    totalSegments: number
    identifiedSpeakers: Set<string>
    sessionDuration: number
    averageConfidence: number
  }

  // Raw Deepgram access
  setRawDeepgramCallback: (callback: (data: any) => void) => void
}

export const useSpeakerWebSocket = (
  initialOptions: SpeakerWebSocketOptions = {}
): UseSpeakerWebSocketReturn => {
  // Connection state
  const [isConnected, setIsConnected] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'disconnected' | 'error'>('disconnected')

  // Streaming state
  const [isStreaming, setIsStreaming] = useState(false)
  const [transcriptSegments, setTranscriptSegments] = useState<TranscriptSegment[]>([])

  // Statistics
  const [stats, setStats] = useState({
    totalSegments: 0,
    identifiedSpeakers: new Set<string>(),
    sessionDuration: 0,
    averageConfidence: 0
  })

  // Refs
  const wsServiceRef = useRef<SpeakerWebSocketService | null>(null)
  const segmentIdRef = useRef(0)
  const sessionStartRef = useRef<number>(0)

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

  // Handle utterance boundary and speaker identified events
  const handleSpeakerEvent = useCallback((event: UtteranceBoundaryEvent | any) => {
    const segmentId = `segment_${segmentIdRef.current++}`
    const timestamp = Date.now()

    const segment: TranscriptSegment = {
      id: segmentId,
      timestamp,
      text: event.transcript,
      speaker_name: event.speaker_identification?.speaker_name || null,
      speaker_id: event.speaker_identification?.speaker_id || null,
      confidence: event.speaker_identification?.confidence || 0,
      duration: event.audio_segment.duration,
      status: event.speaker_identification?.status || 'unknown',
      audio_segment: event.audio_segment
    }

    // Handle interim vs final transcripts
    setTranscriptSegments(prev => {
      if (segment.status === 'interim') {
        // Replace the last interim transcript, or add if no interim exists
        const lastSegment = prev[prev.length - 1]
        if (lastSegment?.status === 'interim') {
          // Replace the last interim transcript
          return [...prev.slice(0, -1), segment]
        } else {
          // Add new interim transcript
          return [...prev, segment]
        }
      } else {
        // For final transcripts, always add (and remove any trailing interim)
        const withoutTrailingInterim = prev[prev.length - 1]?.status === 'interim'
          ? prev.slice(0, -1)
          : prev
        return [...withoutTrailingInterim, segment]
      }
    })

    // Update statistics
    setStats(prev => {
      const totalSegments = prev.totalSegments + 1
      const identifiedSpeakers = new Set(prev.identifiedSpeakers)

      if (segment.speaker_name && segment.status === 'identified') {
        identifiedSpeakers.add(segment.speaker_name)
      }

      // Calculate average confidence (only for segments with identification attempts)
      const segments = [...transcriptSegments, segment]
      const segmentsWithConfidence = segments.filter(s => s.confidence > 0)
      const averageConfidence = segmentsWithConfidence.length > 0
        ? segmentsWithConfidence.reduce((sum, s) => sum + s.confidence, 0) / segmentsWithConfidence.length
        : 0

      return {
        totalSegments,
        identifiedSpeakers,
        sessionDuration: Date.now() - sessionStartRef.current,
        averageConfidence
      }
    })
  }, [transcriptSegments])

  // Initialize WebSocket service
  const initializeService = useCallback(() => {
    if (wsServiceRef.current) return

    wsServiceRef.current = new SpeakerWebSocketService({
      ...initialOptions,
      onUtteranceBoundary: handleSpeakerEvent,
      onSpeakerIdentified: handleSpeakerEvent,  // Handle both event types the same way
      onReady: () => {
        console.log('🟢 Speaker WebSocket ready')
      },
      onError: (error) => {
        console.error('❌ Speaker WebSocket error:', error)
      },
      onConnectionStatusChange: (status) => {
        console.log(`🔄 [WS] Connection status changed: ${status}`)
        setConnectionStatus(status)
        const newConnected = status === 'connected'
        console.log(`🔄 [WS] Setting isConnected: ${newConnected}`)
        setIsConnected(newConnected)
      }
    })
  }, [initialOptions, handleSpeakerEvent])

  // Connect to WebSocket
  const connect = useCallback(async () => {
    initializeService()

    if (wsServiceRef.current) {
      try {
        await wsServiceRef.current.connect()
      } catch (error) {
        console.error('Failed to connect to Speaker WebSocket:', error)
        throw error
      }
    }
  }, [initializeService])

  // Disconnect from WebSocket
  const disconnect = useCallback(() => {
    if (wsServiceRef.current) {
      wsServiceRef.current.disconnect()
      wsServiceRef.current = null
    }
    setIsConnected(false)
    setIsStreaming(false)
    setConnectionStatus('disconnected')
  }, [])

  // Start streaming
  const startStreaming = useCallback(() => {
    const serviceConnected = wsServiceRef.current?.connectionStatus === 'connected'
    console.log(`🎙️ [WS] Attempting to start streaming - React isConnected: ${isConnected}, Service connected: ${serviceConnected}`)

    // Check WebSocket service state directly to avoid React state timing issues
    if (!wsServiceRef.current || !serviceConnected) {
      console.warn('Cannot start streaming: WebSocket service not connected')
      return
    }

    sessionStartRef.current = Date.now()
    setIsStreaming(true)
    console.log(`🎙️ [WS] Set isStreaming to true`)
    setTranscriptSegments([])
    setStats({
      totalSegments: 0,
      identifiedSpeakers: new Set(),
      sessionDuration: 0,
      averageConfidence: 0
    })
    console.log('🎙️ Started streaming session')
  }, [isConnected])

  // Stop streaming
  const stopStreaming = useCallback(() => {
    setIsStreaming(false)
    console.log('⏹️ Stopped streaming session')
  }, [])

  // Send audio data
  const sendAudio = useCallback((audioData: ArrayBuffer | Uint8Array): boolean => {
    if (!wsServiceRef.current) {
      console.warn('❌ [sendAudio] No WebSocket service')
      return false
    }

    // Let the WebSocket service handle streaming state checks internally
    // Removed React state dependency to avoid timing issues
    return wsServiceRef.current.sendAudio(audioData)
  }, []) // No dependencies - avoid React state timing issues

  // Clear transcripts
  const clearTranscripts = useCallback(() => {
    setTranscriptSegments([])
    setStats({
      totalSegments: 0,
      identifiedSpeakers: new Set(),
      sessionDuration: 0,
      averageConfidence: 0
    })
    segmentIdRef.current = 0
  }, [])

  // Update settings
  const updateSettings = useCallback((settings: Partial<SpeakerWebSocketOptions>) => {
    if (wsServiceRef.current) {
      wsServiceRef.current.updateOptions(settings)
    }
  }, [])

  // Set raw Deepgram callback
  const setRawDeepgramCallback = useCallback((callback: (data: any) => void) => {
    if (wsServiceRef.current) {
      wsServiceRef.current.updateOptions({ onRawDeepgram: callback })
    }
  }, [])

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

    // Controls
    connect,
    disconnect,
    startStreaming,
    stopStreaming,
    sendAudio,
    clearTranscripts,

    // Settings
    updateSettings,

    // Statistics
    stats,

    // Raw Deepgram access
    setRawDeepgramCallback
  }
}
