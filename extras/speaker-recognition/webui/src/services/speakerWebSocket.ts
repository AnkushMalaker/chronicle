/**
 * WebSocket service for real-time transcription with speaker change detection
 * Connects to the new WebSocket wrapper endpoint on the server
 */

export interface UtteranceBoundaryEvent {
  type: 'utterance_boundary'
  timestamp: number
  audio_segment: {
    start: number
    end: number
    duration: number
  }
  transcript: string
  speaker_identification?: {
    speaker_id: string | null
    speaker_name: string | null
    confidence: number
    status: 'identified' | 'unknown' | 'error' | 'interim'
    error?: string
  }
}

export interface SpeakerWebSocketEvent {
  type: 'ready' | 'utterance_boundary' | 'error' | 'raw_deepgram' | 'speaker_identified'
  message?: string
  timestamp?: number
  data?: any  // For raw_deepgram events
  features?: {  // For ready event
    raw_deepgram_forwarding?: boolean
    speaker_identification?: boolean
    debug_recording?: boolean
  }
  audio_segment?: {
    start: number
    end: number
    duration: number
  }
  transcript?: string
  speaker_identification?: {
    speaker_id: string | null
    speaker_name: string | null
    confidence: number
    status: 'identified' | 'unknown' | 'error' | 'interim'
    error?: string
  }
}

export interface SpeakerWebSocketOptions {
  // Enhancement parameters (not sent to Deepgram)
  userId?: number
  confidenceThreshold?: number
  deepgramApiKey?: string

  // Deepgram parameters (forwarded to Deepgram API)
  model?: string
  language?: string
  encoding?: string
  sample_rate?: number
  channels?: number
  punctuate?: boolean
  smart_format?: boolean
  interim_results?: boolean
  endpointing?: number
  utterance_end_ms?: number
  diarize?: boolean
  multichannel?: boolean
  numerals?: boolean
  profanity_filter?: boolean
  redact?: string[]
  replace?: string
  search?: string[]
  keywords?: string
  keyterm?: string[]
  filler_words?: boolean
  tag?: string
  // ... additional Deepgram parameters can be added as needed

  // Event callbacks
  onUtteranceBoundary?: (event: UtteranceBoundaryEvent) => void
  onSpeakerIdentified?: (event: SpeakerWebSocketEvent) => void
  onReady?: () => void
  onError?: (error: string) => void
  onConnectionStatusChange?: (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void
  onRawDeepgram?: (data: any) => void
}

export class SpeakerWebSocketService {
  private ws: WebSocket | null = null
  private options: SpeakerWebSocketOptions
  private connectionStatus: 'connecting' | 'connected' | 'disconnected' | 'error' = 'disconnected'
  private baseUrl: string

  constructor(options: SpeakerWebSocketOptions = {}) {
    this.options = options

    // Use nginx proxy for WebSocket connections (HTTPS requirement)
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    this.baseUrl = `${protocol}//${host}`
  }

  private setConnectionStatus(status: 'connecting' | 'connected' | 'disconnected' | 'error') {
    console.log(`🔄 [WS Service] Setting connection status: ${this.connectionStatus} → ${status}`)
    this.connectionStatus = status
    this.options.onConnectionStatusChange?.(status)
  }

  async connect(): Promise<void> {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      console.log('WebSocket already connected')
      return
    }

    this.setConnectionStatus('connecting')

    // Build WebSocket URL with all parameters
    const params = new URLSearchParams()

    // Enhancement parameters
    if (this.options.userId) {
      params.set('user_id', this.options.userId.toString())
    }
    if (this.options.confidenceThreshold !== undefined) {
      params.set('confidence_threshold', this.options.confidenceThreshold.toString())
    }

    // Deepgram parameters
    if (this.options.model) params.set('model', this.options.model)
    if (this.options.language) params.set('language', this.options.language)
    if (this.options.encoding) params.set('encoding', this.options.encoding)
    if (this.options.sample_rate) params.set('sample_rate', this.options.sample_rate.toString())
    if (this.options.channels) params.set('channels', this.options.channels.toString())
    if (this.options.punctuate !== undefined) params.set('punctuate', this.options.punctuate.toString())
    if (this.options.smart_format !== undefined) params.set('smart_format', this.options.smart_format.toString())
    if (this.options.interim_results !== undefined) params.set('interim_results', this.options.interim_results.toString())
    if (this.options.endpointing) params.set('endpointing', this.options.endpointing.toString())
    if (this.options.utterance_end_ms) params.set('utterance_end_ms', this.options.utterance_end_ms.toString())
    if (this.options.diarize !== undefined) params.set('diarize', this.options.diarize.toString())
    if (this.options.multichannel !== undefined) params.set('multichannel', this.options.multichannel.toString())
    if (this.options.numerals !== undefined) params.set('numerals', this.options.numerals.toString())
    if (this.options.profanity_filter !== undefined) params.set('profanity_filter', this.options.profanity_filter.toString())
    if (this.options.replace) params.set('replace', this.options.replace)
    if (this.options.keywords) params.set('keywords', this.options.keywords)
    if (this.options.filler_words !== undefined) params.set('filler_words', this.options.filler_words.toString())
    if (this.options.tag) params.set('tag', this.options.tag)
    // Handle array parameters
    if (this.options.redact && this.options.redact.length > 0) {
      params.set('redact', this.options.redact.join(','))
    }
    if (this.options.search && this.options.search.length > 0) {
      params.set('search', this.options.search.join(','))
    }
    if (this.options.keyterm && this.options.keyterm.length > 0) {
      params.set('keyterm', this.options.keyterm.join(','))
    }

    const wsUrl = `${this.baseUrl}/v1/ws_listen?${params.toString()}`

    console.log(`🔌 Connecting to Speaker WebSocket: ${wsUrl}`)
    console.log(`🔧 Base URL: ${this.baseUrl}`)
    console.log(`🔧 Parameters: ${params.toString()}`)

    return new Promise((resolve, reject) => {
      let connectionResolved = false

      try {
        // Use WebSocket subprotocols for API key authentication (like Deepgram does)
        const protocols = this.options.deepgramApiKey ? ['token', this.options.deepgramApiKey] : undefined
        this.ws = new WebSocket(wsUrl, protocols)

        this.ws.onopen = () => {
          console.log('✅ Speaker WebSocket connected - waiting for ready message')
          this.setConnectionStatus('connected')
          // Don't resolve immediately - wait for ready message
        }

        this.ws.onmessage = (event) => {
          try {
            const data: SpeakerWebSocketEvent = JSON.parse(event.data)
            this.handleMessage(data)

            // Resolve promise when ready message is received
            if (data.type === 'ready' && !connectionResolved) {
              connectionResolved = true
              resolve()
            }
          } catch (error) {
            console.error('Failed to parse WebSocket message:', error)
            if (!connectionResolved) {
              connectionResolved = true
              reject(error)
            }
          }
        }

        this.ws.onclose = (event) => {
          console.log(`🔌 Speaker WebSocket closed: ${event.code} ${event.reason}`)
          this.setConnectionStatus('disconnected')
          this.ws = null
        }

        this.ws.onerror = (error) => {
          console.error('❌ Speaker WebSocket error:', error)
          this.setConnectionStatus('error')
          if (!connectionResolved) {
            connectionResolved = true
            reject(error)
          }
        }
      } catch (error) {
        this.setConnectionStatus('error')
        reject(error)
      }
    })
  }

  private handleMessage(data: SpeakerWebSocketEvent) {
    console.log('📨 Received message:', data.type)

    switch (data.type) {
      case 'ready':
        console.log('🟢 WebSocket ready for audio streaming')
        if (data.features) {
          console.log('🎯 Features enabled:', data.features)
        }
        this.options.onReady?.()
        break

      case 'utterance_boundary':
        if (data.transcript && data.audio_segment) {
          const event: UtteranceBoundaryEvent = {
            type: 'utterance_boundary',
            timestamp: data.timestamp || Date.now(),
            audio_segment: data.audio_segment,
            transcript: data.transcript,
            speaker_identification: data.speaker_identification
          }

          console.log(`🎙️ Utterance boundary: "${event.transcript}" (${event.audio_segment.duration.toFixed(2)}s)`)
          if (event.speaker_identification?.speaker_name) {
            console.log(`👤 Speaker: ${event.speaker_identification.speaker_name} (${event.speaker_identification.confidence.toFixed(3)})`)
          }

          this.options.onUtteranceBoundary?.(event)
        }
        break

      case 'speaker_identified':
        if (data.transcript && data.audio_segment) {
          console.log(`🎙️ Speaker identified: "${data.transcript}" (${data.audio_segment.duration.toFixed(2)}s)`)
          if (data.speaker_identification?.speaker_name) {
            console.log(`👤 Speaker: ${data.speaker_identification.speaker_name} (${data.speaker_identification.confidence.toFixed(3)})`)
          }

          this.options.onSpeakerIdentified?.(data)
        }
        break

      case 'error':
        console.error('❌ Server error:', data.message)
        this.options.onError?.(data.message || 'Unknown server error')
        break

      case 'raw_deepgram':
        console.log('🎤 Raw Deepgram event:', data.data?.type || 'unknown')
        this.options.onRawDeepgram?.(data.data)

        // Extract transcript from Deepgram Results and forward as interim transcript
        if (data.data?.type === 'Results' && data.data?.channel?.alternatives?.[0]?.transcript) {
          const transcript = data.data.channel.alternatives[0].transcript
          const confidence = data.data.channel.alternatives[0].confidence || 0
          const isFinal = data.data.is_final || false

          // Only show transcripts with actual content
          if (transcript.trim()) {
            console.log(`📝 ${isFinal ? 'Final' : 'Interim'} transcript: "${transcript}" (${confidence.toFixed(3)})`)

            // Create interim transcript event
            const interimEvent: UtteranceBoundaryEvent = {
              type: 'utterance_boundary',
              timestamp: data.timestamp || Date.now(),
              audio_segment: {
                start: 0, // Deepgram doesn't provide segment timing in Results
                end: 0,
                duration: 0
              },
              transcript,
              speaker_identification: {
                speaker_id: null,
                speaker_name: isFinal ? 'Speaker' : 'Speaking...', // Show interim vs final
                confidence,
                status: isFinal ? 'unknown' : 'interim'
              }
            }

            // Forward as utterance boundary for display
            this.options.onUtteranceBoundary?.(interimEvent)
          }
        }
        break

      default:
        console.warn('Unknown message type:', data)
    }
  }

  sendAudio(audioData: ArrayBuffer | Uint8Array): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('⚠️ Cannot send audio: WebSocket not connected')
      return false
    }

    try {
      this.ws.send(audioData)
      return true
    } catch (error) {
      console.error('❌ Failed to send audio:', error)
      return false
    }
  }

  disconnect(): void {
    if (this.ws) {
      console.log('🔌 Disconnecting Speaker WebSocket')
      this.ws.close()
      this.ws = null
    }
    this.setConnectionStatus('disconnected')
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  get status(): 'connecting' | 'connected' | 'disconnected' | 'error' {
    return this.connectionStatus
  }

  // Update options
  updateOptions(newOptions: Partial<SpeakerWebSocketOptions>): void {
    this.options = { ...this.options, ...newOptions }
  }
}
