import { useState, useRef, useCallback, useEffect } from 'react'
import { BACKEND_URL } from '../services/api'
import { getStorageKey } from '../utils/storage'

export interface ComponentErrors {
  websocket: string | null
  microphone: string | null
  audioContext: string | null
  streaming: string | null
}

export interface DebugStats {
  chunksSent: number
  messagesReceived: number
  lastError: string | null
  lastErrorTime: Date | null
  sessionStartTime: Date | null
  connectionAttempts: number
}

export interface UseAudioRecordingReturn {
  // Connection state
  isWebSocketConnected: boolean
  connectionStatus: 'disconnected' | 'connecting' | 'connected' | 'error'

  // Recording state
  isRecording: boolean
  recordingDuration: number
  audioProcessingStarted: boolean

  // Component states (direct checks, no sync issues)
  hasValidWebSocket: boolean
  hasValidMicrophone: boolean
  hasValidAudioContext: boolean
  isCurrentlyStreaming: boolean

  // Granular test states
  hasMicrophoneAccess: boolean
  hasAudioContext: boolean
  isStreaming: boolean

  // Error management
  error: string | null
  componentErrors: ComponentErrors

  // Debug information
  debugStats: DebugStats

  // Actions
  connectWebSocketOnly: () => Promise<boolean>
  disconnectWebSocketOnly: () => void
  sendAudioStartOnly: () => Promise<boolean>
  sendAudioStopOnly: () => Promise<boolean>
  requestMicrophoneOnly: () => Promise<boolean>
  createAudioContextOnly: () => Promise<boolean>
  startStreamingOnly: () => Promise<boolean>
  stopStreamingOnly: () => boolean
  testFullFlowOnly: () => Promise<boolean>
  startRecording: () => Promise<void>
  stopRecording: () => void

  // Utilities
  formatDuration: (seconds: number) => string
  canAccessMicrophone: boolean
}

export const useAudioRecording = (): UseAudioRecordingReturn => {
  // Basic state
  const [isRecording, setIsRecording] = useState(false)
  const [isWebSocketConnected, setIsWebSocketConnected] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected')
  const [recordingDuration, setRecordingDuration] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [audioProcessingStarted, setAudioProcessingStarted] = useState(false)

  // Granular testing states
  const [hasMicrophoneAccess, setHasMicrophoneAccess] = useState(false)
  const [hasAudioContext, setHasAudioContext] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)

  // Error tracking
  const [componentErrors, setComponentErrors] = useState<ComponentErrors>({
    websocket: null,
    microphone: null,
    audioContext: null,
    streaming: null
  })

  // Debug stats
  const [debugStats, setDebugStats] = useState<DebugStats>({
    chunksSent: 0,
    messagesReceived: 0,
    lastError: null,
    lastErrorTime: null,
    sessionStartTime: null,
    connectionAttempts: 0
  })

  // Refs for direct access (no state sync issues)
  const wsRef = useRef<WebSocket | null>(null)
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const durationIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const keepAliveIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const audioProcessingStartedRef = useRef(false)
  const chunkCountRef = useRef(0)
  // Note: Legacy message queue code removed as it was unused

  // Check if we're on localhost or using HTTPS
  const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  const isHttps = window.location.protocol === 'https:'
  const canAccessMicrophone = isLocalhost || isHttps

  // Direct status checks (no state sync issues)
  const hasValidWebSocket = wsRef.current?.readyState === WebSocket.OPEN
  const hasValidMicrophone = mediaStreamRef.current !== null
  const hasValidAudioContext = audioContextRef.current !== null
  const isCurrentlyStreaming = isStreaming && hasValidWebSocket && hasValidMicrophone

  const connectWebSocket = useCallback(async () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return true
    }

    setConnectionStatus('connecting')
    setError(null)

    try {
      const token = localStorage.getItem(getStorageKey('token'))
      if (!token) {
        throw new Error('No authentication token found')
      }

      // Build WebSocket URL using BACKEND_URL from API service (handles base path correctly)
        const { protocol } = window.location
        const wsProtocol = protocol === 'https:' ? 'wss:' : 'ws:'

        let wsUrl: string
        if (BACKEND_URL && BACKEND_URL.startsWith('http')) {
          // BACKEND_URL is a full URL (e.g., http://localhost:8000)
          const backendHost = BACKEND_URL.replace(/^https?:\/\//, '')
          wsUrl = `${wsProtocol}//${backendHost}/ws?codec=pcm&token=${token}&device_name=webui-recorder`
        } else if (BACKEND_URL && BACKEND_URL !== '') {
          // BACKEND_URL is a path (e.g., /prod)
          wsUrl = `${wsProtocol}//${window.location.host}${BACKEND_URL}/ws?codec=pcm&token=${token}&device_name=webui-recorder`
        } else {
          // BACKEND_URL is empty (same origin)
          wsUrl = `${wsProtocol}//${window.location.host}/ws?codec=pcm&token=${token}&device_name=webui-recorder`
        }
      const ws = new WebSocket(wsUrl)
      // Note: Don't set binaryType yet - will cause protocol violations with text messages

      return new Promise<boolean>((resolve, reject) => {
        ws.onopen = () => {
          console.log('🎤 WebSocket connected for live recording')
          setConnectionStatus('connected')
          setIsWebSocketConnected(true)

          // Add stabilization delay before resolving to prevent protocol violations
          setTimeout(() => {
            wsRef.current = ws
            setDebugStats(prev => ({
              ...prev,
              sessionStartTime: new Date(),
              connectionAttempts: prev.connectionAttempts + 1
            }))

            // Start keepalive ping every 30 seconds
            keepAliveIntervalRef.current = setInterval(() => {
              if (ws.readyState === WebSocket.OPEN) {
                try {
                  // Send a Wyoming protocol ping event
                  const ping = { type: 'ping', payload_length: null }
                  ws.send(JSON.stringify(ping) + '\n')
                } catch (e) {
                  console.error('Failed to send keepalive ping:', e)
                }
              }
            }, 30000)

            console.log('🔌 WebSocket stabilized and ready for messages')
            resolve(true)
          }, 100) // 100ms stabilization delay
        }

        ws.onclose = (event) => {
          console.log('🎤 WebSocket disconnected:', event.code, event.reason)
          setConnectionStatus('disconnected')
          setIsWebSocketConnected(false)
          wsRef.current = null

          // Clear keepalive interval
          if (keepAliveIntervalRef.current) {
            clearInterval(keepAliveIntervalRef.current)
            keepAliveIntervalRef.current = undefined
          }

          if (isRecording) {
            stopRecording()
          }
        }

        ws.onerror = (error) => {
          console.error('🎤 WebSocket error:', error)
          setConnectionStatus('error')
          const errorMsg = 'Failed to connect to backend'
          setError(errorMsg)
          setComponentErrors(prev => ({ ...prev, websocket: errorMsg }))
          reject(error)
        }

        ws.onmessage = (event) => {
          // Handle any messages from the server
          console.log('🎤 Received message from server:', event.data)
          setDebugStats(prev => ({ ...prev, messagesReceived: prev.messagesReceived + 1 }))
        }

        // Timeout after 5 seconds
        setTimeout(() => {
          if (ws.readyState !== WebSocket.OPEN) {
            ws.close()
            reject(new Error('Connection timeout'))
          }
        }, 5000)
      })
    } catch (error) {
      console.error('Failed to connect WebSocket:', error)
      setConnectionStatus('error')
      const errorMsg = error instanceof Error ? error.message : 'Connection failed'
      setError(errorMsg)
      setComponentErrors(prev => ({ ...prev, websocket: errorMsg }))
      return false
    }
  }, [isRecording])

  const connectWebSocketOnly = async () => {
    if (isWebSocketConnected) {
      console.log('🔌 WebSocket already connected')
      return true
    }

    try {
      setError(null)
      setComponentErrors(prev => ({ ...prev, websocket: null }))
      const connected = await connectWebSocket()
      if (connected) {
        setComponentErrors(prev => ({ ...prev, websocket: null }))
      }
      return connected
    } catch (error) {
      console.error('Failed to connect WebSocket:', error)
      const errorMsg = error instanceof Error ? error.message : 'Connection failed'
      setError(errorMsg)
      setComponentErrors(prev => ({ ...prev, websocket: errorMsg }))
      return false
    }
  }

  const disconnectWebSocketOnly = () => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setIsWebSocketConnected(false)
    setConnectionStatus('disconnected')
    console.log('🔌 WebSocket disconnected manually')
  }

  const sendAudioStartOnly = async () => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      setError('WebSocket not connected')
      return false
    }

    try {
      const startMessage = {
        type: 'audio-start',
        data: {
          rate: 16000,
          width: 2,
          channels: 1
        },
        payload_length: null
      }
      wsRef.current.send(JSON.stringify(startMessage) + '\n')
      console.log('📤 Sent audio-start message (standalone)')
      return true
    } catch (error) {
      console.error('Failed to send audio-start:', error)
      setError(error instanceof Error ? error.message : 'Failed to send audio-start')
      return false
    }
  }

  const sendAudioStopOnly = async () => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      setError('WebSocket not connected')
      return false
    }

    try {
      const stopMessage = {
        type: 'audio-stop',
        data: {
          timestamp: Date.now()
        },
        payload_length: null
      }
      wsRef.current.send(JSON.stringify(stopMessage) + '\n')
      console.log('📤 Sent audio-stop message (standalone)')
      return true
    } catch (error) {
      console.error('Failed to send audio-stop:', error)
      setError(error instanceof Error ? error.message : 'Failed to send audio-stop')
      return false
    }
  }

  // Granular testing functions
  const requestMicrophoneOnly = async () => {
    try {
      setComponentErrors(prev => ({ ...prev, microphone: null }))

      if (!canAccessMicrophone) {
        throw new Error('Microphone access requires HTTPS or localhost')
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      })

      // Clean up the stream immediately - we just wanted to test permissions
      stream.getTracks().forEach(track => track.stop())

      setHasMicrophoneAccess(true)
      console.log('🎤 Microphone access granted')
      return true
    } catch (error) {
      console.error('Failed to get microphone access:', error)
      const errorMsg = error instanceof Error ? error.message : 'Microphone access denied'
      setComponentErrors(prev => ({ ...prev, microphone: errorMsg }))
      setHasMicrophoneAccess(false)
      return false
    }
  }

  const createAudioContextOnly = async () => {
    try {
      setComponentErrors(prev => ({ ...prev, audioContext: null }))

      if (audioContextRef.current) {
        audioContextRef.current.close()
      }

      const audioContext = new AudioContext({ sampleRate: 16000 })
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 256

      audioContextRef.current = audioContext
      analyserRef.current = analyser

      setHasAudioContext(true)
      console.log('📊 Audio context created successfully')
      return true
    } catch (error) {
      console.error('Failed to create audio context:', error)
      const errorMsg = error instanceof Error ? error.message : 'Audio context creation failed'
      setComponentErrors(prev => ({ ...prev, audioContext: errorMsg }))
      setHasAudioContext(false)
      return false
    }
  }

  const startStreamingOnly = async () => {
    try {
      setComponentErrors(prev => ({ ...prev, streaming: null }))

      // Use direct checks instead of state
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        throw new Error('WebSocket not connected')
      }

      // Check if microphone access was previously tested
      if (!hasMicrophoneAccess) {
        throw new Error('Microphone access test required first - click "Get Mic" button')
      }

      // Check if audio context was previously created
      if (!hasAudioContext) {
        throw new Error('Audio context test required first - click "Create Context" button')
      }

      // Get microphone stream
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      })

      mediaStreamRef.current = stream

      // Connect to audio context
      if (audioContextRef.current && analyserRef.current) {
        const source = audioContextRef.current.createMediaStreamSource(stream)
        source.connect(analyserRef.current)

        // Set up audio processing
        const processor = audioContextRef.current.createScriptProcessor(4096, 1, 1)
        source.connect(processor)
        processor.connect(audioContextRef.current.destination)

        processor.onaudioprocess = (event) => {
          if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
            return
          }

          if (!audioProcessingStartedRef.current) {
            console.log('🚫 Audio processing not started yet, skipping chunk')
            return
          }

          const inputBuffer = event.inputBuffer
          const inputData = inputBuffer.getChannelData(0)

          // Convert float32 to int16 PCM
          const pcmBuffer = new Int16Array(inputData.length)
          for (let i = 0; i < inputData.length; i++) {
            const sample = Math.max(-1, Math.min(1, inputData[i]))
            pcmBuffer[i] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF
          }

          try {
            const chunkHeader = {
              type: 'audio-chunk',
              data: {
                rate: 16000,
                width: 2,
                channels: 1
              },
              payload_length: pcmBuffer.byteLength
            }

            // Set binary type for WebSocket before sending binary data
            if (wsRef.current.binaryType !== 'arraybuffer') {
              wsRef.current.binaryType = 'arraybuffer'
              console.log('🔧 Set WebSocket binaryType to arraybuffer for audio chunks')
            }

            wsRef.current.send(JSON.stringify(chunkHeader) + '\n')
            wsRef.current.send(new Uint8Array(pcmBuffer.buffer, pcmBuffer.byteOffset, pcmBuffer.byteLength))

            // Update debug stats
            chunkCountRef.current++
            setDebugStats(prev => ({ ...prev, chunksSent: chunkCountRef.current }))
          } catch (error) {
            console.error('Failed to send audio chunk:', error)
            setDebugStats(prev => ({
              ...prev,
              lastError: error instanceof Error ? error.message : 'Chunk send failed',
              lastErrorTime: new Date()
            }))
          }
        }

        processorRef.current = processor
      }

      setIsStreaming(true)
      console.log('🎵 Audio streaming started')
      return true
    } catch (error) {
      console.error('Failed to start streaming:', error)
      const errorMsg = error instanceof Error ? error.message : 'Streaming failed'
      setComponentErrors(prev => ({ ...prev, streaming: errorMsg }))
      setIsStreaming(false)
      return false
    }
  }

  const stopStreamingOnly = () => {
    try {
      // Clean up media stream
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach(track => track.stop())
        mediaStreamRef.current = null
      }

      // Clean up processor
      if (processorRef.current) {
        processorRef.current.disconnect()
        processorRef.current = null
      }

      setIsStreaming(false)
      console.log('🎵 Audio streaming stopped')
      return true
    } catch (error) {
      console.error('Failed to stop streaming:', error)
      return false
    }
  }

  const testFullFlowOnly = async () => {
    try {
      setError(null)
      console.log('💾 Starting full flow test...')

      // Step 1: Connect WebSocket
      const connected = await connectWebSocket()
      if (!connected) {
        throw new Error('WebSocket connection failed')
      }

      // Step 2: Get microphone access
      const micAccess = await requestMicrophoneOnly()
      if (!micAccess) {
        throw new Error('Microphone access failed')
      }

      // Step 3: Create audio context
      const contextCreated = await createAudioContextOnly()
      if (!contextCreated) {
        throw new Error('Audio context creation failed')
      }

      // Step 4: Send audio-start
      const startSent = await sendAudioStartOnly()
      if (!startSent) {
        throw new Error('Audio-start message failed')
      }

      // Step 5: Start streaming for 10 seconds
      const streamingStarted = await startStreamingOnly()
      if (!streamingStarted) {
        throw new Error('Audio streaming failed')
      }

      console.log('💾 Full flow test running for 10 seconds...')

      // Wait 10 seconds
      setTimeout(() => {
        stopStreamingOnly()
        sendAudioStopOnly()
        console.log('💾 Full flow test completed')
      }, 10000)

      return true
    } catch (error) {
      console.error('Full flow test failed:', error)
      setError(error instanceof Error ? error.message : 'Full flow test failed')
      return false
    }
  }

  const startRecording = async () => {
    try {
      setError(null)

      if (!canAccessMicrophone) {
        setError('Microphone access requires either localhost access or HTTPS connection due to browser security restrictions')
        return
      }

      // Connect WebSocket first
      const connected = await connectWebSocket()
      if (!connected) {
        return
      }

      // Get user media
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      })

      mediaStreamRef.current = stream

      // Set up audio context and analyser for visualization
      const audioContext = new AudioContext({ sampleRate: 16000 })
      const analyser = audioContext.createAnalyser()
      const source = audioContext.createMediaStreamSource(stream)

      analyser.fftSize = 256
      source.connect(analyser)

      audioContextRef.current = audioContext
      analyserRef.current = analyser

      // Send Wyoming protocol start message FIRST
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        try {
          const startMessage = {
            type: 'audio-start',
            data: {
              rate: 16000,
              width: 2,
              channels: 1
            },
            payload_length: null
          }
          wsRef.current.send(JSON.stringify(startMessage) + '\n')
          console.log('🎤 Sent audio-start message')
        } catch (error) {
          console.error('Failed to send audio-start:', error)
          throw error
        }
      } else {
        throw new Error('WebSocket not connected')
      }

      // Enable audio processing after a delay to ensure backend processes audio-start
      setTimeout(() => {
        // Set up audio processing for WebSocket AFTER the delay
        const processor = audioContext.createScriptProcessor(4096, 1, 1)
        source.connect(processor)
        processor.connect(audioContext.destination)

        processor.onaudioprocess = (event) => {
          if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
            return
          }

          // Don't send audio chunks until audio-start has been sent and processed
          if (!audioProcessingStartedRef.current) {
            console.log('🚫 Audio processing not started yet, skipping chunk')
            return
          }

          const inputBuffer = event.inputBuffer
          const inputData = inputBuffer.getChannelData(0)

          // Convert float32 to int16 PCM
          const pcmBuffer = new Int16Array(inputData.length)
          for (let i = 0; i < inputData.length; i++) {
            const sample = Math.max(-1, Math.min(1, inputData[i]))
            pcmBuffer[i] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF
          }

          try {
            // Send Wyoming protocol audio chunk
            const chunkHeader = {
              type: 'audio-chunk',
              data: {
                rate: 16000,
                width: 2,
                channels: 1
              },
              payload_length: pcmBuffer.byteLength
            }

            // Set binary type for WebSocket before sending binary data
            if (wsRef.current.binaryType !== 'arraybuffer') {
              wsRef.current.binaryType = 'arraybuffer'
              console.log('🔧 Set WebSocket binaryType to arraybuffer for audio chunks')
            }

            // Send header + binary data
            wsRef.current.send(JSON.stringify(chunkHeader) + '\n')
            // Send the actual Int16Array buffer, not the underlying ArrayBuffer
            wsRef.current.send(new Uint8Array(pcmBuffer.buffer, pcmBuffer.byteOffset, pcmBuffer.byteLength))

            // Update debug stats
            chunkCountRef.current++
            setDebugStats(prev => ({ ...prev, chunksSent: chunkCountRef.current }))
          } catch (error) {
            console.error('Failed to send audio chunk:', error)
            setDebugStats(prev => ({
              ...prev,
              lastError: error instanceof Error ? error.message : 'Chunk send failed',
              lastErrorTime: new Date()
            }))
          }
        }

        processorRef.current = processor
        setAudioProcessingStarted(true)
        audioProcessingStartedRef.current = true
        console.log('🎵 Audio processing enabled after delay')
      }, 500) // Increased delay from 100ms to 500ms

      setIsRecording(true)
      setRecordingDuration(0)

      // Start duration timer
      durationIntervalRef.current = setInterval(() => {
        setRecordingDuration(prev => prev + 1)
      }, 1000)

      console.log('🎤 Recording started')

    } catch (error) {
      console.error('Failed to start recording:', error)
      setError(error instanceof Error ? error.message : 'Failed to start recording')
      setConnectionStatus('error')
    }
  }

  const stopRecording = () => {
    try {
      // Stop audio processing first
      setAudioProcessingStarted(false)
      audioProcessingStartedRef.current = false
      console.log('🛑 Audio processing disabled')

      // Send Wyoming protocol stop message
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        try {
          const stopMessage = {
            type: 'audio-stop',
            data: {
              timestamp: Date.now()
            },
            payload_length: null
          }
          wsRef.current.send(JSON.stringify(stopMessage) + '\n')
          console.log('🎤 Sent audio-stop message')
        } catch (error) {
          console.error('Failed to send audio-stop:', error)
        }
      }

      // Clean up media stream
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach(track => track.stop())
        mediaStreamRef.current = null
      }

      // Clean up audio context
      if (processorRef.current) {
        processorRef.current.disconnect()
        processorRef.current = null
      }

      if (audioContextRef.current) {
        audioContextRef.current.close()
        audioContextRef.current = null
      }

      analyserRef.current = null

      // Clear duration timer
      if (durationIntervalRef.current) {
        clearInterval(durationIntervalRef.current)
        durationIntervalRef.current = undefined
      }

      setIsRecording(false)
      console.log('🎤 Recording stopped')

    } catch (error) {
      console.error('Error stopping recording:', error)
      setError('Error stopping recording')
    }
  }

  const formatDuration = (seconds: number) => {
    const minutes = Math.floor(seconds / 60)
    const remainingSeconds = seconds % 60
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`
  }

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (isRecording) {
        stopRecording()
      }
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [isRecording])

  return {
    // Connection state
    isWebSocketConnected,
    connectionStatus,

    // Recording state
    isRecording,
    recordingDuration,
    audioProcessingStarted,

    // Direct status checks (no state sync issues)
    hasValidWebSocket,
    hasValidMicrophone,
    hasValidAudioContext,
    isCurrentlyStreaming,

    // Granular test states
    hasMicrophoneAccess,
    hasAudioContext,
    isStreaming,

    // Error management
    error,
    componentErrors,

    // Debug information
    debugStats,

    // Actions
    connectWebSocketOnly,
    disconnectWebSocketOnly,
    sendAudioStartOnly,
    sendAudioStopOnly,
    requestMicrophoneOnly,
    createAudioContextOnly,
    startStreamingOnly,
    stopStreamingOnly,
    testFullFlowOnly,
    startRecording,
    stopRecording,

    // Utilities
    formatDuration,
    canAccessMicrophone,

    // Internal refs for components that need them
    analyserRef
  } as UseAudioRecordingReturn & { analyserRef: React.RefObject<AnalyserNode | null> }
}
