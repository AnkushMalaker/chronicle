/**
 * SpeakerIdentificationService - Unified speaker identification operations
 * Consolidates all speaker identification modes and API calls
 * Supports diarization-only, Deepgram enhanced, hybrid, and plain identification
 */

import { apiService } from './api'
import { transcribeWithDeepgram, processDeepgramResponse, calculateConfidenceSummary, DeepgramResponse } from './deepgram'

export type ProcessingMode = 'diarization-only' | 'speaker-identification' | 'deepgram-enhanced' | 'deepgram-transcript-internal-speakers' | 'diarize-identify-match'

export interface SpeakerSegment {
  start: number
  end: number
  speaker_id: string
  speaker_name: string
  confidence: number
  text?: string
  identified_speaker_id?: string
  identified_speaker_name?: string
  speaker_identification_confidence?: number
  speaker_status?: string
}

export interface ProcessingOptions {
  mode: ProcessingMode
  userId?: number
  confidenceThreshold?: number
  minDuration?: number
  minSpeakers?: number
  maxSpeakers?: number
  identifyOnlyEnrolled?: boolean
  enhanceSpeakers?: boolean
  transcriptData?: any  // For diarize-identify-match mode
  collar?: number  // Collar duration for merging segments
  minDurationOff?: number  // Minimum silence duration for boundaries
}

export interface ProcessingResult {
  id: string
  filename: string
  duration: number
  status: 'processing' | 'completed' | 'failed'
  created_at: string
  mode: ProcessingMode
  speakers: SpeakerSegment[]
  confidence_summary: {
    total_segments: number
    high_confidence: number
    medium_confidence: number
    low_confidence: number
  }
  deepgram_response?: DeepgramResponse
  processing_time?: number
  error?: string
}

export interface IdentifyResult {
  found: boolean
  speaker_id: string | null
  speaker_name: string | null
  confidence: number
  status: string
  similarity_threshold: number
  duration: number
}

export class SpeakerIdentificationService {
  private static instance: SpeakerIdentificationService

  static getInstance(): SpeakerIdentificationService {
    if (!SpeakerIdentificationService.instance) {
      SpeakerIdentificationService.instance = new SpeakerIdentificationService()
    }
    return SpeakerIdentificationService.instance
  }

  /**
   * Process audio using specified mode
   */
  async processAudio(
    audioFile: File | Blob,
    options: ProcessingOptions
  ): Promise<ProcessingResult> {
    const startTime = Date.now()

    try {
      let result: ProcessingResult

      switch (options.mode) {
        case 'deepgram-enhanced':
          result = await this.processWithDeepgram(audioFile, options)
          break
        case 'deepgram-transcript-internal-speakers':
          result = await this.processWithHybrid(audioFile, options)
          break
        case 'diarization-only':
          result = await this.processWithDiarizationOnly(audioFile, options)
          break
        case 'diarize-identify-match':
          result = await this.processWithDiarizeIdentifyMatch(audioFile, options)
          break
        case 'speaker-identification':
        default:
          result = await this.processWithDiarization(audioFile, options)
          break
      }

      result.processing_time = Date.now() - startTime
      return result

    } catch (error) {
      // Provide more helpful error messages based on the error type
      let errorMessage = `Processing failed: ${error instanceof Error ? error.message : 'Unknown error'}`

      if (error.message?.includes('500') || error.message?.includes('Internal Server Error')) {
        errorMessage = `Server error during ${options.mode} processing. This might be due to a backend issue. Please try again or contact support.`
      } else if (error.message?.includes('404') || error.message?.includes('Not Found')) {
        errorMessage = `Processing endpoint not available. The ${options.mode} mode might not be fully implemented yet.`
      } else if (error.message?.includes('400') || error.message?.includes('Bad Request')) {
        errorMessage = `Bad request during ${options.mode} processing. This might be due to invalid audio format or missing transcript data. Please check your input files.`
      } else if (error.message?.includes('timeout')) {
        errorMessage = `Processing timed out. The audio file might be too large or the server is busy. Please try a shorter audio file.`
      } else if (error.message?.includes('transcript data is required')) {
        errorMessage = `Transcript data is required for ${options.mode} mode. Please upload a Deepgram JSON file first.`
      } else if (error.message?.includes('Failed to transform Deepgram data')) {
        errorMessage = `Invalid Deepgram JSON format. Please ensure you've uploaded a valid Deepgram API response file.`
      }

      throw new Error(errorMessage)
    }
  }

  /**
   * Simple speaker identification for single utterances
   */
  async identifyUtterance(
    audioFile: File | Blob,
    options: Partial<ProcessingOptions> = {}
  ): Promise<IdentifyResult> {
    try {
      const formData = new FormData()
      formData.append('file', audioFile, 'utterance.wav')
      formData.append('similarity_threshold', (options.confidenceThreshold || 0.15).toString())

      if (options.userId) {
        formData.append('user_id', options.userId.toString())
      }

      const response = await apiService.post('/identify', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 15000
      })

      return response.data as IdentifyResult
    } catch (error) {
      return {
        found: false,
        speaker_id: null,
        speaker_name: null,
        confidence: 0,
        status: 'error',
        similarity_threshold: options.confidenceThreshold || 0.15,
        duration: 0
      }
    }
  }

  /**
   * Process with Deepgram transcription + speaker enhancement
   */
  private async processWithDeepgram(
    audioFile: File | Blob,
    options: ProcessingOptions
  ): Promise<ProcessingResult> {
    try {
      const filename = audioFile instanceof File ? audioFile.name : 'Audio'

      // Use shared Deepgram service
      const deepgramResponse = await transcribeWithDeepgram(audioFile, {
        enhanceSpeakers: options.enhanceSpeakers !== false,
        userId: options.userId,
        speakerConfidenceThreshold: options.confidenceThreshold || 0.15,
        mode: 'standard'
      })

      // Process response using shared service
      const deepgramSegments = processDeepgramResponse(deepgramResponse)

      // Convert to SpeakerSegment format
      const speakerSegments: SpeakerSegment[] = deepgramSegments.map(segment => ({
        start: segment.start,
        end: segment.end,
        speaker_id: segment.speakerId || `speaker_${segment.speaker}`,
        speaker_name: segment.speakerName || `Speaker ${segment.speaker}`,
        confidence: segment.confidence,
        text: segment.text,
        identified_speaker_id: segment.identifiedSpeakerId,
        identified_speaker_name: segment.identifiedSpeakerName,
        speaker_identification_confidence: segment.speakerIdentificationConfidence,
        speaker_status: segment.speakerStatus
      }))

      // Calculate confidence summary
      const confidenceSummary = calculateConfidenceSummary(deepgramSegments)

      return {
        id: Math.random().toString(36),
        filename,
        duration: this.estimateDuration(speakerSegments),
        status: 'completed',
        created_at: new Date().toISOString(),
        mode: 'deepgram-enhanced',
        speakers: speakerSegments,
        confidence_summary: confidenceSummary,
        deepgram_response: deepgramResponse
      }
    } catch (error) {
      throw new Error(`Deepgram processing failed: ${error.message}`)
    }
  }

  /**
   * Process with hybrid mode (Deepgram transcription + internal diarization)
   * Step 1: Transcribe audio via /v1/listen (Deepgram)
   * Step 2: Send audio + transcript to /v1/diarize-identify-match for speaker diarization & identification
   */
  private async processWithHybrid(
    audioFile: File | Blob,
    options: ProcessingOptions
  ): Promise<ProcessingResult> {
    try {
      const filename = audioFile instanceof File ? audioFile.name : 'Audio'

      // Step 1: Transcribe with Deepgram via /v1/listen
      const deepgramResponse = await transcribeWithDeepgram(audioFile, {
        enhanceSpeakers: false, // Speaker enhancement done by internal diarization
        userId: options.userId,
        speakerConfidenceThreshold: options.confidenceThreshold || 0.15,
        mode: 'standard'
      })

      // Step 2: Send audio + transcript to diarize-identify-match
      const transcriptData = this.transformDeepgramForDiarizeMatch(deepgramResponse)

      const formData = new FormData()
      formData.append('file', audioFile)
      formData.append('transcript_data', transcriptData)

      if (options.userId) {
        formData.append('user_id', options.userId.toString())
      }
      if (options.minDuration !== undefined) {
        formData.append('min_duration', options.minDuration.toString())
      }
      if (options.confidenceThreshold !== undefined) {
        formData.append('similarity_threshold', options.confidenceThreshold.toString())
      }
      if (options.minSpeakers !== undefined) {
        formData.append('min_speakers', options.minSpeakers.toString())
      }
      if (options.maxSpeakers !== undefined) {
        formData.append('max_speakers', options.maxSpeakers.toString())
      }
      if (options.collar !== undefined) {
        formData.append('collar', options.collar.toString())
      }
      if (options.minDurationOff !== undefined) {
        formData.append('min_duration_off', options.minDurationOff.toString())
      }

      const response = await apiService.post('/v1/diarize-identify-match', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 300000
      })

      const backendSegments = response.data.segments || []

      // Convert backend format to frontend format
      const speakers: SpeakerSegment[] = backendSegments.map((segment: any) => ({
        start: segment.start,
        end: segment.end,
        speaker_id: segment.speaker_id || segment.speaker,
        speaker_name: segment.identified_as || `Unknown (${segment.speaker})`,
        confidence: segment.confidence || 0,
        text: segment.text
      }))

      // Calculate confidence summary
      const high_confidence = speakers.filter(s => s.confidence >= 0.8).length
      const medium_confidence = speakers.filter(s => s.confidence >= 0.6 && s.confidence < 0.8).length
      const low_confidence = speakers.filter(s => s.confidence >= 0.4 && s.confidence < 0.6).length

      return {
        id: Math.random().toString(36),
        filename,
        duration: this.estimateDuration(speakers),
        status: 'completed',
        created_at: new Date().toISOString(),
        mode: 'deepgram-transcript-internal-speakers',
        speakers,
        confidence_summary: {
          total_segments: speakers.length,
          high_confidence,
          medium_confidence,
          low_confidence
        },
        deepgram_response: deepgramResponse
      }
    } catch (error) {
      throw new Error(`Hybrid processing failed: ${error.message}`)
    }
  }

  /**
   * Process with diarization-only (no speaker identification)
   */
  private async processWithDiarizationOnly(
    audioFile: File | Blob,
    options: ProcessingOptions
  ): Promise<ProcessingResult> {
    try {
      const filename = audioFile instanceof File ? audioFile.name : 'Audio'

      const formData = new FormData()
      formData.append('file', audioFile)
      formData.append('min_duration', (options.minDuration || 0.5).toString())

      if (options.minSpeakers) {
        formData.append('min_speakers', options.minSpeakers.toString())
      }
      if (options.maxSpeakers) {
        formData.append('max_speakers', options.maxSpeakers.toString())
      }

      // Use the diarize-only endpoint
      const response = await apiService.post('/v1/diarize-only', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 180000
      })

      const backendSegments = response.data.segments || []

      // Convert backend format to frontend format
      const speakers: SpeakerSegment[] = backendSegments.map((segment: any) => ({
        start: segment.start,
        end: segment.end,
        speaker_id: segment.speaker,  // Use generic speaker ID
        speaker_name: segment.speaker,  // Use generic speaker name
        confidence: 0  // No identification confidence for diarization-only
        // No text in diarization-only mode
      }))

      // Calculate confidence summary (all zero for diarization-only)
      return {
        id: Math.random().toString(36),
        filename,
        duration: this.estimateDuration(speakers),
        status: 'completed',
        created_at: new Date().toISOString(),
        mode: 'diarization-only',
        speakers,
        confidence_summary: {
          total_segments: speakers.length,
          high_confidence: 0,
          medium_confidence: 0,
          low_confidence: 0
        }
      }
    } catch (error) {
      throw new Error(`Diarization-only processing failed: ${error.message}`)
    }
  }

  /**
   * Process with original diarization endpoint (now speaker identification)
   */
  private async processWithDiarization(
    audioFile: File | Blob,
    options: ProcessingOptions
  ): Promise<ProcessingResult> {
    try {
      const filename = audioFile instanceof File ? audioFile.name : 'Audio'

      const formData = new FormData()
      formData.append('file', audioFile)

      // Use query parameters instead of form data (like the working /v1/listen endpoint)
      const params = {
        similarity_threshold: (options.confidenceThreshold || 0.15).toString(),
        min_duration: (options.minDuration || 1.0).toString(),
        identify_only_enrolled: (options.identifyOnlyEnrolled || false).toString(),
        ...(options.userId && { user_id: options.userId.toString() }),
        ...(options.minSpeakers && { min_speakers: options.minSpeakers.toString() }),
        ...(options.maxSpeakers && { max_speakers: options.maxSpeakers.toString() }),
        ...(options.collar !== undefined && { collar: options.collar.toString() }),
        ...(options.minDurationOff !== undefined && { min_duration_off: options.minDurationOff.toString() })
      }

      // Ensure we have a valid file
      if (!audioFile || audioFile.size === 0) {
        throw new Error('Invalid audio file: file is empty or null')
      }

      const response = await apiService.post('/diarize-and-identify', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        params: params,  // Send as query parameters
        timeout: 180000
      })

      // Process the diarization response
      const backendSegments = response.data.segments || []

      // Convert backend format to frontend format
      const speakers: SpeakerSegment[] = backendSegments.map((segment: any) => ({
        start: segment.start,
        end: segment.end,
        speaker_id: segment.speaker_id || segment.speaker,
        speaker_name: segment.identified_as || `Unknown (${segment.speaker})`,
        confidence: segment.confidence || 0
        // No text in diarization-only mode
      }))

      // Calculate confidence summary
      const high_confidence = speakers.filter(s => s.confidence >= 0.8).length
      const medium_confidence = speakers.filter(s => s.confidence >= 0.6 && s.confidence < 0.8).length
      const low_confidence = speakers.filter(s => s.confidence >= 0.4 && s.confidence < 0.6).length

      return {
        id: Math.random().toString(36),
        filename,
        duration: this.estimateDuration(speakers),
        status: 'completed',
        created_at: new Date().toISOString(),
        mode: 'speaker-identification',
        speakers,
        confidence_summary: {
          total_segments: speakers.length,
          high_confidence,
          medium_confidence,
          low_confidence
        }
      }
    } catch (error) {
      throw new Error(`Diarization processing failed: ${error.message}`)
    }
  }


  /**
   * Process with diarize-identify-match (transcript + internal diarization + speaker ID)
   */
  private async processWithDiarizeIdentifyMatch(
    audioFile: File | Blob,
    options: ProcessingOptions
  ): Promise<ProcessingResult> {
    try {
      const filename = audioFile instanceof File ? audioFile.name : 'Audio'

      if (!options.transcriptData) {
        throw new Error('Transcript data is required for diarize-identify-match mode')
      }

      // Transform Deepgram JSON to expected format
      const transformedTranscriptData = this.transformDeepgramForDiarizeMatch(options.transcriptData)

      const formData = new FormData()
      formData.append('file', audioFile)
      formData.append('transcript_data', transformedTranscriptData)

      // Add speaker processing parameters
      if (options.userId) {
        formData.append('user_id', options.userId.toString())
      }
      if (options.minDuration !== undefined) {
        formData.append('min_duration', options.minDuration.toString())
      }
      if (options.confidenceThreshold !== undefined) {
        formData.append('similarity_threshold', options.confidenceThreshold.toString())
      }
      if (options.minSpeakers !== undefined) {
        formData.append('min_speakers', options.minSpeakers.toString())
      }
      if (options.maxSpeakers !== undefined) {
        formData.append('max_speakers', options.maxSpeakers.toString())
      }
      if (options.collar !== undefined) {
        formData.append('collar', options.collar.toString())
      }
      if (options.minDurationOff !== undefined) {
        formData.append('min_duration_off', options.minDurationOff.toString())
      }

      const response = await apiService.post('/v1/diarize-identify-match', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 180000
      })

      const backendSegments = response.data.segments || []

      // Convert backend format to frontend format
      const speakers: SpeakerSegment[] = backendSegments.map((segment: any) => ({
        start: segment.start,
        end: segment.end,
        speaker_id: segment.speaker_id || segment.speaker,
        speaker_name: segment.identified_as || `Unknown (${segment.speaker})`,
        confidence: segment.confidence || 0,
        text: segment.text // This mode includes matched transcript text
      }))

      // Calculate confidence summary
      const high_confidence = speakers.filter(s => s.confidence >= 0.8).length
      const medium_confidence = speakers.filter(s => s.confidence >= 0.6 && s.confidence < 0.8).length
      const low_confidence = speakers.filter(s => s.confidence >= 0.4 && s.confidence < 0.6).length

      return {
        id: Math.random().toString(36),
        filename,
        duration: this.estimateDuration(speakers),
        status: 'completed',
        created_at: new Date().toISOString(),
        mode: 'diarize-identify-match',
        speakers,
        confidence_summary: {
          total_segments: speakers.length,
          high_confidence,
          medium_confidence,
          low_confidence
        }
      }
    } catch (error) {
      throw new Error(`Diarize-identify-match processing failed: ${error.message}`)
    }
  }

  /**
   * Get available processing modes
   */
  getAvailableModes(): Array<{ mode: ProcessingMode; name: string; description: string }> {
    return [
      {
        mode: 'diarization-only',
        name: 'Diarization Only',
        description: 'Pure speaker diarization (Speaker A, Speaker B) - no identification or transcription'
      },
      {
        mode: 'speaker-identification',
        name: 'Speaker Identification',
        description: 'Diarization + identify enrolled speakers - no transcription'
      },
      {
        mode: 'deepgram-enhanced',
        name: 'Deepgram Enhanced',
        description: 'Deepgram transcription + diarization + replace speakers with enrolled IDs'
      },
      {
        mode: 'deepgram-transcript-internal-speakers',
        name: 'Deepgram Transcript + Internal Speakers',
        description: 'Deepgram transcription + internal diarization + speaker identification'
      },
      {
        mode: 'diarize-identify-match',
        name: 'Transcript + Diarize Match',
        description: 'Match existing transcript to internal diarization + speaker identification'
      }
    ]
  }

  /**
   * Transform Deepgram JSON to the format expected by diarize-identify-match API
   */
  private transformDeepgramForDiarizeMatch(deepgramData: any): string {
    try {
      // Extract words from Deepgram format: results.channels[0].alternatives[0].words
      let words: Array<{word: string, start: number, end: number}> = []
      let fullText = ""

      if (deepgramData?.results?.channels?.[0]?.alternatives?.[0]) {
        const alternative = deepgramData.results.channels[0].alternatives[0]

        // Extract words with timestamps
        if (alternative.words) {
          words = alternative.words.map((wordData: any) => ({
            word: wordData.word || wordData.punctuated_word || '',
            start: parseFloat(wordData.start || 0),
            end: parseFloat(wordData.end || 0)
          })).filter((word: any) => word.word) // Remove empty words
        }

        // Extract full transcript
        fullText = alternative.transcript || ''
      }

      // Format for backend API
      const formattedData = {
        words: words,
        text: fullText
      }

      return JSON.stringify(formattedData)
    } catch (error) {
      throw new Error(`Failed to transform Deepgram data: ${error instanceof Error ? error.message : 'Unknown error'}`)
    }
  }

  /**
   * Estimate duration from speaker segments
   */
  private estimateDuration(segments: SpeakerSegment[]): number {
    if (segments.length === 0) return 0
    return Math.max(...segments.map(s => s.end))
  }
}

// Export singleton instance
export const speakerIdentificationService = SpeakerIdentificationService.getInstance()
