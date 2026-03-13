import { apiService } from './api'

export interface DeepgramSegment {
  start: number
  end: number
  duration: number
  speaker: number
  speakerId?: string
  speakerName?: string
  confidence: number
  text: string
  identifiedSpeakerId?: string
  identifiedSpeakerName?: string
  speakerIdentificationConfidence?: number
  speakerStatus?: string
}

export interface DeepgramTranscriptionOptions {
  model?: string
  language?: string
  diarize?: boolean
  smartFormat?: boolean
  punctuate?: boolean
  enhanceSpeakers?: boolean
  userId?: number
  speakerConfidenceThreshold?: number
  mode?: 'standard' | 'hybrid' | 'live'
  minDuration?: number
  enablePlainMode?: boolean
}

export interface DeepgramResponse {
  results: {
    channels: Array<{
      alternatives: Array<{
        words: Array<{
          start: number
          end: number
          word: string
          punctuated_word?: string
          speaker?: number
          confidence?: number
          identified_speaker_id?: string
          identified_speaker_name?: string
          speaker_identification_confidence?: number
          speaker_status?: string
        }>
        transcript: string
      }>
    }>
  }
  speaker_enhancement?: {
    enabled: boolean
    method?: string
    user_id?: number
    confidence_threshold?: number
    identified_speakers?: any[]
    total_segments?: number
    identified_segments?: number
    total_speakers?: number
    identified_count?: number
  }
}

/**
 * Default options for Deepgram transcription requests
 */
export const DEFAULT_DEEPGRAM_OPTIONS: DeepgramTranscriptionOptions = {
  model: 'nova-3',
  language: 'multi',
  diarize: true,
  smartFormat: true,
  punctuate: true,
  enhanceSpeakers: true,
  speakerConfidenceThreshold: 0.15,
  mode: 'standard'
}

/**
 * Transcribe audio file using Deepgram API
 */
export async function transcribeWithDeepgram(
  file: File | Blob,
  options: DeepgramTranscriptionOptions = {}
): Promise<DeepgramResponse> {
  const opts = { ...DEFAULT_DEEPGRAM_OPTIONS, ...options }

  const formData = new FormData()
  formData.append('file', file)

  // Always use /v1/listen for transcription
  // Hybrid mode (transcribe + internal diarization) is handled by speakerIdentification service
  const endpoint = '/v1/listen'

  const params: Record<string, any> = {
    model: opts.model,
    language: opts.language,
    diarize: opts.diarize,
    smart_format: opts.smartFormat,
    punctuate: opts.punctuate
  }

  // Add enhancement parameters if enabled
  if (opts.enhanceSpeakers) {
    params.enhance_speakers = true
    params.user_id = opts.userId
    params.speaker_confidence_threshold = opts.speakerConfidenceThreshold
  }

  // Plain mode parameter for internal processing
  if (opts.enablePlainMode) {
    params.enable_plain_mode = true
  }

  const response = await apiService.post(endpoint, formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
    params,
    timeout: 300000, // 5 minutes for transcription operations
  })

  return response.data as DeepgramResponse
}

/**
 * Group words from Deepgram response by speaker into segments
 */
export function groupWordsBySpeaker(words: DeepgramResponse['results']['channels'][0]['alternatives'][0]['words']): DeepgramSegment[] {
  if (!words || words.length === 0) return []

  const segments: DeepgramSegment[] = []
  let currentSegment: any = null

  for (const word of words) {
    const speaker = word.speaker || 0

    if (!currentSegment || currentSegment.speaker !== speaker) {
      // Start new segment
      if (currentSegment) {
        segments.push(currentSegment)
      }
      currentSegment = {
        start: word.start,
        end: word.end,
        duration: 0, // Will be calculated when segment ends
        speaker,
        speakerId: word.identified_speaker_id || `speaker_${speaker}`,
        speakerName: word.identified_speaker_name || `Speaker ${speaker}`,
        confidence: word.speaker_identification_confidence || 0, // Use speaker identification confidence, not word confidence
        text: word.punctuated_word || word.word,
        identifiedSpeakerId: word.identified_speaker_id,
        identifiedSpeakerName: word.identified_speaker_name,
        speakerIdentificationConfidence: word.speaker_identification_confidence,
        speakerStatus: word.speaker_status
      }
    } else {
      // Continue current segment
      currentSegment.end = word.end
      currentSegment.text += ' ' + (word.punctuated_word || word.word)
    }
  }

  // Add final segment
  if (currentSegment) {
    segments.push(currentSegment)
  }

  // Calculate durations
  segments.forEach(segment => {
    segment.duration = segment.end - segment.start
  })

  return segments
}

/**
 * Process Deepgram response and extract speaker segments
 */
export function processDeepgramResponse(response: DeepgramResponse): DeepgramSegment[] {
  const results = response.results || {}
  const channels = results.channels || []

  if (!channels.length) return []

  const channel = channels[0]
  const alternatives = channel.alternatives || []

  if (!alternatives.length) return []

  const words = alternatives[0].words || []
  return groupWordsBySpeaker(words)
}

/**
 * Calculate confidence summary for segments
 */
export function calculateConfidenceSummary(segments: DeepgramSegment[]) {
  const total = segments.length
  const high = segments.filter(s => s.confidence >= 0.8).length
  const medium = segments.filter(s => s.confidence >= 0.6 && s.confidence < 0.8).length
  const low = segments.filter(s => s.confidence >= 0.4 && s.confidence < 0.6).length
  const veryLow = total - high - medium - low

  return {
    total_segments: total,
    high_confidence: high,
    medium_confidence: medium,
    low_confidence: low,
    very_low_confidence: veryLow
  }
}

/**
 * Convert Deepgram segments to annotation format
 * Used by the Annotation page to convert transcription results
 */
export function convertToAnnotationSegments(segments: DeepgramSegment[]): Array<{
  id: string
  start: number
  end: number
  duration: number
  speakerLabel?: string
  deepgramSpeakerLabel?: string
  label: 'CORRECT' | 'INCORRECT' | 'UNCERTAIN'
  confidence?: number
  transcription?: string
}> {
  return segments.map(segment => ({
    id: Math.random().toString(36),
    start: segment.start,
    end: segment.end,
    duration: segment.duration,
    speakerLabel: segment.speakerName,
    deepgramSpeakerLabel: `speaker_${segment.speaker}`,
    label: 'UNCERTAIN' as const,
    confidence: segment.confidence,
    transcription: segment.text
  }))
}

// ============================================================================
// STREAMING SUPPORT
// ============================================================================

export interface StreamingConfig {
  apiKey: string
  model?: string
  language?: string
  punctuate?: boolean
  diarize?: boolean
  interim_results?: boolean
  endpointing?: number
  vad_events?: boolean
  utterance_end_ms?: number
  encoding?: string
  sample_rate?: number
}

export interface SpeechStartedEvent {
  type: 'SpeechStarted'
  channel: number[]
  timestamp: number
}

export interface UtteranceEndEvent {
  type: 'UtteranceEnd'
  channel: number[]
  last_word_end: number
}

export interface StreamingTranscript {
  transcript: string
  confidence: number
  words: Array<{
    word: string
    start: number
    end: number
    confidence: number
    speaker?: number
    punctuated_word?: string
  }>
  is_final: boolean
}

export class DeepgramStreaming {
  private ws: WebSocket | null = null
  private isConnected = false
  private config: StreamingConfig
  private onTranscriptCallback?: (transcript: StreamingTranscript) => void
  private onErrorCallback?: (error: Error) => void
  private onStatusCallback?: (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void
  private onSpeechStartedCallback?: (event: SpeechStartedEvent) => void
  private onUtteranceEndCallback?: (event: UtteranceEndEvent) => void
  private connectionTime?: number
  private firstAudioTime?: number
  private audioPacketCount = 0

  constructor(config: StreamingConfig) {
    this.config = {
      model: 'nova-2',
      language: 'en-US',
      punctuate: true,
      diarize: true,
      interim_results: true,
      endpointing: 300,
      vad_events: true,
      utterance_end_ms: 1000,
      encoding: 'linear16',
      sample_rate: 16000,
      ...config
    }
  }

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        console.log('🌐 [DEEPGRAM] Connecting to Deepgram...')
        this.onStatusCallback?.('connecting')

        // Validate API key format
        if (!this.config.apiKey || this.config.apiKey.length < 30) {
          const error = new Error('Invalid Deepgram API key format')
          console.error('🌐 [DEEPGRAM] API key validation failed:', this.config.apiKey?.length, 'characters')
          reject(error)
          return
        }

        // Include ALL configuration parameters for VAD events and full functionality
        const params = new URLSearchParams({
          model: this.config.model!,
          language: this.config.language!,
          smart_format: 'true',
          encoding: this.config.encoding || 'linear16',
          sample_rate: (this.config.sample_rate || 16000).toString()
        })

        // Add VAD and speech detection parameters
        if (this.config.interim_results) {
          params.append('interim_results', 'true')
        }
        if (this.config.vad_events) {
          params.append('vad_events', 'true')
        }
        if (this.config.utterance_end_ms) {
          params.append('utterance_end_ms', this.config.utterance_end_ms.toString())
        }
        if (this.config.endpointing) {
          params.append('endpointing', this.config.endpointing.toString())
        }

        // Create WebSocket URL without token (token goes in subprotocol)
        const wsUrl = `wss://api.deepgram.com/v1/listen?${params.toString()}`

        console.log('🌐 [DEEPGRAM] WebSocket URL:', wsUrl)
        console.log('🌐 [DEEPGRAM] VAD Configuration:', {
          interim_results: this.config.interim_results,
          vad_events: this.config.vad_events,
          utterance_end_ms: this.config.utterance_end_ms,
          endpointing: this.config.endpointing,
          language: this.config.language
        })
        console.log('🌐 [DEEPGRAM] API key length:', this.config.apiKey.length)
        console.log('🌐 [DEEPGRAM] API key format check:', /^[a-f0-9]+$/i.test(this.config.apiKey))

        // Create WebSocket connection with subprotocol authentication (official method)
        this.ws = new WebSocket(wsUrl, ['token', this.config.apiKey])

        console.log('🌐 [DEEPGRAM] WebSocket created with subprotocol auth, readyState:', this.ws.readyState)

        // This is already handled above in the timeout section
        // this.ws.onopen = () => {
        //   console.log('🌐 [DEEPGRAM] ✅ Connected!')
        //   this.isConnected = true
        //   this.connectionTime = Date.now()
        //   this.onStatusCallback?.('connected')
        //   resolve()
        // }

        this.ws.onclose = (event) => {
          console.log('🌐 [DEEPGRAM] WebSocket connection closed:', {
            code: event.code,
            reason: event.reason,
            wasClean: event.wasClean
          })
          console.log('🌐 [DEEPGRAM] Setting isConnected = false')
          this.isConnected = false
          console.log('🌐 [DEEPGRAM] Calling status callback with "disconnected"')
          this.onStatusCallback?.('disconnected')
        }

        this.ws.onerror = (error) => {
          console.error('🌐 [DEEPGRAM] ❌ WebSocket error occurred:', error)
          console.error('🌐 [DEEPGRAM] Error event details:', {
            type: error.type,
            target: (error.target as WebSocket)?.readyState,
            currentTarget: (error.currentTarget as WebSocket)?.readyState
          })
          console.log('🌐 [DEEPGRAM] Setting isConnected = false due to error')
          this.isConnected = false
          console.log('🌐 [DEEPGRAM] Calling status callback with "error"')
          this.onStatusCallback?.('error')

          const err = new Error(`WebSocket connection failed. Check your Deepgram API key and internet connection.`)
          console.error('🌐 [DEEPGRAM] Calling error callback with error:', err.message)
          this.onErrorCallback?.(err)
          console.error('🌐 [DEEPGRAM] Rejecting connection promise with error')
          reject(err)
        }

        this.ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data)

            // Enhanced logging for VAD events and transcripts
            if (data.type === 'Results' && data.channel?.alternatives?.[0]?.transcript) {
              console.log('📝 [DEEPGRAM] TRANSCRIPT:', data.channel.alternatives[0].transcript, {
                is_final: data.is_final,
                confidence: data.channel.alternatives[0].confidence
              })
            } else if (data.type === 'Metadata') {
              console.log('🌐 [DEEPGRAM] ✅ Connected - ready for audio')
            } else if (data.type === 'SpeechStarted') {
              console.log('🎙️ [VAD] 🗣️ SPEECH STARTED at', data.timestamp, 'channel:', data.channel)
              this.onSpeechStartedCallback?.(data as SpeechStartedEvent)
            } else if (data.type === 'UtteranceEnd') {
              console.log('🔚 [VAD] 🛑 UTTERANCE ENDED at', data.last_word_end, 'channel:', data.channel)
              this.onUtteranceEndCallback?.(data as UtteranceEndEvent)
            } else {
              // Log any other message types we might be receiving
              console.log('🌐 [DEEPGRAM] Other message:', data.type, data)
            }

            // Handle different message types
            if (data.type === 'Results' && data.channel?.alternatives?.[0]) {
              const transcript = data.channel.alternatives[0]

              // Only emit if we have actual content
              if (transcript.transcript && transcript.transcript.trim()) {
                this.onTranscriptCallback?.({
                  transcript: transcript.transcript,
                  confidence: transcript.confidence || 0,
                  words: transcript.words || [],
                  is_final: data.is_final || false
                })
              }
            }
          } catch (error) {
            console.error('🌐 [DEEPGRAM] ❌ Error parsing message:', error)
            console.error('🌐 [DEEPGRAM] Raw message data:', event.data)
            console.error('🌐 [DEEPGRAM] Parse error details:', {
              message: error instanceof Error ? error.message : String(error),
              stack: error instanceof Error ? error.stack : 'No stack'
            })
            const parseError = new Error(`Failed to parse message: ${error}`)
            console.error('🌐 [DEEPGRAM] Calling error callback with parse error')
            this.onErrorCallback?.(parseError)
          }
        }

        // Shorter connection timeout for faster feedback
        console.log('🌐 [DEEPGRAM] Setting 5-second connection timeout')
        const timeoutId = setTimeout(() => {
          if (!this.isConnected && this.ws) {
            console.error('🌐 [DEEPGRAM] ⏰ Connection timeout after 5 seconds')
            console.error('🌐 [DEEPGRAM] WebSocket readyState at timeout:', this.ws?.readyState)

            // Clean up the failed connection
            if (this.ws) {
              this.ws.close()
              this.ws = null
            }

            const timeoutError = new Error('Connection timeout - Deepgram did not respond within 5 seconds. Check your API key and internet connection.')
            console.error('🌐 [DEEPGRAM] Rejecting with timeout error')
            this.onStatusCallback?.('error')
            reject(timeoutError)
          } else {
            console.log('🌐 [DEEPGRAM] Connection established before timeout')
          }
        }, 5000)

        // Clear timeout if connection succeeds
        this.ws.onopen = () => {
          clearTimeout(timeoutId)
          console.log('🌐 [DEEPGRAM] ✅ Connected!')
          this.isConnected = true
          this.connectionTime = Date.now()
          this.onStatusCallback?.('connected')
          resolve()
        }

      } catch (error) {
        console.error('🌐 [DEEPGRAM] ❌ Exception in connect() method:', error)
        console.error('🌐 [DEEPGRAM] Exception details:', {
          message: error instanceof Error ? error.message : String(error),
          stack: error instanceof Error ? error.stack : 'No stack'
        })
        console.error('🌐 [DEEPGRAM] Rejecting with caught exception')
        reject(error)
      }
    })
  }

  disconnect(): void {
    console.log('🌐 [DEEPGRAM] Disconnect called')
    if (this.ws) {
      console.log('🌐 [DEEPGRAM] Closing WebSocket connection, current readyState:', this.ws.readyState)
      this.ws.close()
      this.ws = null
      console.log('🌐 [DEEPGRAM] WebSocket closed and reference cleared')
    } else {
      console.log('🌐 [DEEPGRAM] No WebSocket to close')
    }
    console.log('🌐 [DEEPGRAM] Setting isConnected = false')
    this.isConnected = false
  }

  sendAudio(audioData: ArrayBuffer): void {
    if (this.ws && this.isConnected && this.ws.readyState === WebSocket.OPEN) {
      this.audioPacketCount++

      // Track first audio packet timing
      if (!this.firstAudioTime && this.connectionTime) {
        this.firstAudioTime = Date.now()
        const delayMs = this.firstAudioTime - this.connectionTime
        console.log('🌐 [DEEPGRAM] 🎵 First audio packet sent!')
        console.log('🌐 [DEEPGRAM] ⏱️ Time since connection:', delayMs + 'ms')
        console.log('🌐 [DEEPGRAM] Audio packet size:', audioData.byteLength, 'bytes')
        if (delayMs > 8000) {
          console.warn('🌐 [DEEPGRAM] ⚠️ Audio delay > 8s - may cause timeout!')
        }
      }

      // Minimal logging for audio packets
      if (this.audioPacketCount % 200 === 0) {
        console.log('🌐 [DEEPGRAM] Audio packets sent:', this.audioPacketCount)
      }

      this.ws.send(audioData)
    } else {
      console.warn('🌐 [DEEPGRAM] ⚠️ Cannot send audio - WebSocket not connected:', {
        wsExists: !!this.ws,
        isConnected: this.isConnected,
        readyState: this.ws?.readyState,
        audioPacketCount: this.audioPacketCount
      })
    }
  }

  finishStream(): void {
    if (this.ws && this.isConnected && this.ws.readyState === WebSocket.OPEN) {
      // Send empty message to signal end of stream
      this.ws.send(JSON.stringify({ type: 'CloseStream' }))
    }
  }

  onTranscript(callback: (transcript: StreamingTranscript) => void): void {
    this.onTranscriptCallback = callback
  }

  onError(callback: (error: Error) => void): void {
    this.onErrorCallback = callback
  }

  onStatus(callback: (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void): void {
    this.onStatusCallback = callback
  }

  onSpeechStarted(callback: (event: SpeechStartedEvent) => void): void {
    this.onSpeechStartedCallback = callback
  }

  onUtteranceEnd(callback: (event: UtteranceEndEvent) => void): void {
    this.onUtteranceEndCallback = callback
  }

  getConnectionStatus(): 'connecting' | 'connected' | 'disconnected' | 'error' {
    if (!this.ws) return 'disconnected'

    switch (this.ws.readyState) {
      case WebSocket.CONNECTING:
        return 'connecting'
      case WebSocket.OPEN:
        return 'connected'
      case WebSocket.CLOSING:
      case WebSocket.CLOSED:
        return 'disconnected'
      default:
        return 'error'
    }
  }
}

/**
 * Utility function to convert audio samples to the format expected by Deepgram
 */
export function convertAudioForDeepgram(audioBuffer: Float32Array, sampleRate: number = 16000): ArrayBuffer {
  // Convert float32 samples to int16
  const int16Array = new Int16Array(audioBuffer.length)

  for (let i = 0; i < audioBuffer.length; i++) {
    // Clamp to [-1, 1] and convert to 16-bit integer
    const sample = Math.max(-1, Math.min(1, audioBuffer[i]))
    int16Array[i] = sample * 0x7FFF
  }

  return int16Array.buffer
}

/**
 * Resample audio to 16kHz if needed
 */
export function resampleAudio(
  audioBuffer: Float32Array,
  originalSampleRate: number,
  targetSampleRate: number = 16000
): Float32Array {
  if (originalSampleRate === targetSampleRate) {
    return audioBuffer
  }

  const ratio = originalSampleRate / targetSampleRate
  const newLength = Math.round(audioBuffer.length / ratio)
  const resampled = new Float32Array(newLength)

  for (let i = 0; i < newLength; i++) {
    const originalIndex = i * ratio
    const index = Math.floor(originalIndex)
    const fraction = originalIndex - index

    if (index + 1 < audioBuffer.length) {
      // Linear interpolation
      resampled[i] = audioBuffer[index] * (1 - fraction) + audioBuffer[index + 1] * fraction
    } else {
      resampled[i] = audioBuffer[index] || 0
    }
  }

  return resampled
}
