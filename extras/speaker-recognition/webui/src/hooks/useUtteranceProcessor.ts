/**
 * Custom hook for processing utterances and extracting exact audio segments
 */

import { useRef, useCallback } from 'react'
import { UtteranceEndEvent } from '../services/deepgram'
import { calculateBufferIndices, extractAudioSegmentFromBuffers } from '../utils/audioUtils'
import { utteranceLogger } from '../utils/logger'

export interface UseUtteranceProcessorReturn {
  // State refs
  utteranceTranscriptsRef: React.MutableRefObject<string[]>
  currentUtteranceSegmentIds: React.MutableRefObject<string[]>
  utteranceStartTimeRef: React.MutableRefObject<number | null>
  streamStartTimeRef: React.MutableRefObject<number | null>
  processingUtteranceRef: React.MutableRefObject<boolean>

  // Functions
  resetRefs: () => void
  extractUtteranceAudio: (
    event: UtteranceEndEvent,
    audioBuffers: Float32Array[]
  ) => Promise<{ audioBuffer: Float32Array; segmentIds: string[]; transcripts: string[] } | null>

  // Tracking functions
  trackUtteranceStart: (startTime: number) => void
  addUtteranceSegment: (transcript: string, segmentId: string) => void
}

export function useUtteranceProcessor(): UseUtteranceProcessorReturn {
  // Refs for utterance tracking
  const utteranceTranscriptsRef = useRef<string[]>([])
  const currentUtteranceSegmentIds = useRef<string[]>([])
  const utteranceStartTimeRef = useRef<number | null>(null)
  const streamStartTimeRef = useRef<number | null>(null)
  const processingUtteranceRef = useRef<boolean>(false)

  // Helper function to reset utterance refs
  const resetRefs = useCallback(() => {
    utteranceTranscriptsRef.current = []
    currentUtteranceSegmentIds.current = []
    utteranceStartTimeRef.current = null
    processingUtteranceRef.current = false
  }, [])

  // Track the start of an utterance
  const trackUtteranceStart = useCallback((startTime: number) => {
    if (utteranceTranscriptsRef.current.length === 0) {
      utteranceStartTimeRef.current = startTime
      utteranceLogger.info('⏰ Utterance started at:', startTime)
    }
  }, [])

  // Add a segment to the current utterance
  const addUtteranceSegment = useCallback((transcript: string, segmentId: string) => {
    utteranceTranscriptsRef.current.push(transcript)
    currentUtteranceSegmentIds.current.push(segmentId)

    utteranceLogger.info('Collected segment:', transcript)
    utteranceLogger.info('Segment ID:', segmentId)
    utteranceLogger.info('Total segments so far:', utteranceTranscriptsRef.current.length)
    utteranceLogger.info('Current segment IDs:', currentUtteranceSegmentIds.current)
  }, [])

  // Extract exact audio for the utterance
  const extractUtteranceAudio = useCallback(async (
    event: UtteranceEndEvent,
    audioBuffers: Float32Array[]
  ): Promise<{ audioBuffer: Float32Array; segmentIds: string[]; transcripts: string[] } | null> => {

    // Prevent duplicate processing
    if (processingUtteranceRef.current) {
      utteranceLogger.warn('⚠️ Already processing an utterance, skipping duplicate event')
      return null
    }

    utteranceLogger.info('Utterance ended, triggering speaker identification')
    utteranceLogger.info('Collected transcripts:', utteranceTranscriptsRef.current)
    utteranceLogger.info('Collected segment IDs:', currentUtteranceSegmentIds.current)

    // Validate we have data to process
    if (utteranceTranscriptsRef.current.length === 0) {
      utteranceLogger.info('No transcripts to process')
      return null
    }

    if (currentUtteranceSegmentIds.current.length === 0) {
      utteranceLogger.warn('⚠️ No segment IDs collected - this is unexpected!')
      utteranceLogger.warn('This might be a timing issue or duplicate utterance event')
      return null
    }

    processingUtteranceRef.current = true

    // Capture segment IDs locally to prevent race conditions
    const segmentIdsToUpdate = [...currentUtteranceSegmentIds.current]
    const transcriptsToProcess = [...utteranceTranscriptsRef.current]

    utteranceLogger.info('Captured for processing:', {
      segmentIds: segmentIdsToUpdate,
      transcripts: transcriptsToProcess
    })

    try {
      // Calculate utterance timing and extract the exact audio segment
      const utteranceStartTime = utteranceStartTimeRef.current
      const utteranceEndTime = event.last_word_end
      const streamStartTime = streamStartTimeRef.current

      if (!utteranceStartTime || !streamStartTime) {
        utteranceLogger.error('Missing timing information - cannot extract utterance audio')
        utteranceLogger.error('utteranceStartTime:', utteranceStartTime, 'streamStartTime:', streamStartTime)
        resetRefs()
        return null
      }

      // Calculate which buffers correspond to the utterance timing
      const utteranceDuration = utteranceEndTime - utteranceStartTime
      const bufferDurationMs = 256 // Each buffer is ~256ms (4096 samples at 16kHz)

      // Calculate buffer indices based on stream-relative timing
      const { startIndex, endIndex, isValid } = calculateBufferIndices(
        utteranceStartTime,
        utteranceEndTime,
        bufferDurationMs,
        audioBuffers.length
      )

      utteranceLogger.info('Timing calculation:', {
        utteranceStartTime,
        utteranceEndTime,
        utteranceDuration,
        streamStartTime,
        startIndex,
        endIndex,
        totalBuffers: audioBuffers.length
      })

      if (!isValid) {
        utteranceLogger.error('Invalid buffer range - cannot extract utterance audio')
        utteranceLogger.error('startBufferIndex:', startIndex, 'endBufferIndex:', endIndex, 'totalBuffers:', audioBuffers.length)
        resetRefs()
        return null
      }

      // Extract the exact utterance audio segment
      const audioBuffer = extractAudioSegmentFromBuffers(audioBuffers, startIndex, endIndex)

      if (!audioBuffer) {
        utteranceLogger.error('No utterance buffers in calculated range')
        resetRefs()
        return null
      }

      utteranceLogger.info('✅ Exact utterance audio buffer created:', {
        bufferCount: endIndex - startIndex,
        totalSamples: audioBuffer.length,
        durationSeconds: audioBuffer.length / 16000,
        calculatedDuration: utteranceDuration,
        startIndex,
        endIndex
      })

      return {
        audioBuffer,
        segmentIds: segmentIdsToUpdate,
        transcripts: transcriptsToProcess
      }

    } catch (error) {
      utteranceLogger.error('Error during utterance audio extraction:', error)
      resetRefs()
      return null
    }
  }, [resetRefs])

  return {
    utteranceTranscriptsRef,
    currentUtteranceSegmentIds,
    utteranceStartTimeRef,
    streamStartTimeRef,
    processingUtteranceRef,
    resetRefs,
    extractUtteranceAudio,
    trackUtteranceStart,
    addUtteranceSegment
  }
}
