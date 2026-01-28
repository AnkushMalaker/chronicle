import { useState, useRef, useCallback, useEffect } from 'react'
import { BACKEND_URL } from '../services/api'
import { getStorageKey } from '../utils/storage'

export type RecordingStep = 'idle' | 'mic' | 'websocket' | 'audio-start' | 'streaming' | 'stopping' | 'error'
export type RecordingMode = 'batch' | 'streaming'

export interface DebugStats {
  chunksSent: number
  messagesReceived: number
  lastError: string | null
  lastErrorTime: Date | null
  sessionStartTime: Date | null
  connectionAttempts: number
}

export interface SimpleAudioRecordingReturn {
  // Current state
  currentStep: RecordingStep
  isRecording: boolean
  recordingDuration: number
  error: string | null
  mode: RecordingMode

  // Actions
  startRecording: () => Promise<void>
  stopRecording: () => void
  setMode: (mode: RecordingMode) => void

  // For components
  analyser: AnalyserNode | null
  debugStats: DebugStats

  // Utilities
  formatDuration: (seconds: number) => string
  canAccessMicrophone: boolean
}

export const useSimpleAudioRecording = (): SimpleAudioRecordingReturn => {
  // Basic state
  const [currentStep, setCurrentStep] = useState<RecordingStep>('idle')
  const [isRecording, setIsRecording] = useState(false)
  const [recordingDuration, setRecordingDuration] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<RecordingMode>('streaming')
  
  // Debug stats
  const [debugStats, setDebugStats] = useState<DebugStats>({
    chunksSent: 0,
    messagesReceived: 0,
    lastError: null,
    lastErrorTime: null,
    sessionStartTime: null,
    connectionAttempts: 0
  })
  
  // Refs for direct access
  const wsRef = useRef<WebSocket | null>(null)
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const durationIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const keepAliveIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const chunkCountRef = useRef(0)
  const audioProcessingStartedRef = useRef(false)
  
  // Note: user was unused and removed
  
  // Check if we're on localhost or using HTTPS
  const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  const isHttps = window.location.protocol === 'https:'

  // DEVELOPMENT ONLY: Allow specific IP addresses (remove in production!)
  const devAllowedHosts = import.meta.env.MODE === 'development'
    ? ['192.168.1.100', '10.0.0.100'] // Add your Docker host IPs here
    : []
  const isDevelopmentHost = devAllowedHosts.includes(window.location.hostname)

  const canAccessMicrophone = isLocalhost || isHttps || isDevelopmentHost
  
  // Format duration helper
  const formatDuration = useCallback((seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }, [])
  
  // Cleanup function
  const cleanup = useCallback(() => {
    console.log('🧹 Cleaning up audio recording resources')
    
    // Stop audio processing
    audioProcessingStartedRef.current = false
    
    // Clean up media stream
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop())
      mediaStreamRef.current = null
    }
    
    // Clean up audio context
    if (audioContextRef.current?.state !== 'closed') {
      audioContextRef.current?.close()
    }
    audioContextRef.current = null
    analyserRef.current = null
    processorRef.current = null
    
    // Clean up WebSocket
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    
    // Clear intervals
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current)
      durationIntervalRef.current = undefined
    }
    
    if (keepAliveIntervalRef.current) {
      clearInterval(keepAliveIntervalRef.current)
      keepAliveIntervalRef.current = undefined
    }
    
    // Reset counters
    chunkCountRef.current = 0
  }, [])
  
  // Step 1: Get microphone access
  const getMicrophoneAccess = useCallback(async (): Promise<MediaStream> => {
    console.log('🎤 Step 1: Requesting microphone access')
    
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
    
    mediaStreamRef.current = stream
    console.log('✅ Microphone access granted')
    return stream
  }, [canAccessMicrophone])
  
  // Step 2: Connect WebSocket
  const connectWebSocket = useCallback(async (): Promise<WebSocket> => {
    console.log('🔗 Step 2: Connecting to WebSocket')

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
    
    return new Promise<WebSocket>((resolve, reject) => {
      const ws = new WebSocket(wsUrl)
      // Don't set binaryType yet - only when needed for audio chunks
      
      ws.onopen = () => {
        console.log('🔌 WebSocket connected')
        
        // Add stabilization delay before resolving
        setTimeout(() => {
          wsRef.current = ws
          setDebugStats(prev => ({ 
            ...prev, 
            connectionAttempts: prev.connectionAttempts + 1,
            sessionStartTime: new Date()
          }))
          
          // Start keepalive ping every 30 seconds
          keepAliveIntervalRef.current = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
              try {
                const ping = { type: 'ping', payload_length: null }
                ws.send(JSON.stringify(ping) + '\n')
              } catch (e) {
                console.error('Failed to send keepalive ping:', e)
              }
            }
          }, 30000)
          
          console.log('✅ WebSocket stabilized and ready')
          resolve(ws)
        }, 100) // 100ms stabilization delay
      }
      
      ws.onclose = (event) => {
        console.log('🔌 WebSocket disconnected:', event.code, event.reason)
        wsRef.current = null
        
        if (keepAliveIntervalRef.current) {
          clearInterval(keepAliveIntervalRef.current)
          keepAliveIntervalRef.current = undefined
        }
      }
      
      ws.onerror = (error) => {
        console.error('🔌 WebSocket error:', error)
        reject(new Error('Failed to connect to backend'))
      }
      
      ws.onmessage = (event) => {
        console.log('📨 Received message from server:', event.data)
        setDebugStats(prev => ({ ...prev, messagesReceived: prev.messagesReceived + 1 }))

        // Parse server messages
        try {
          const message = JSON.parse(event.data)

          // Handle error messages from backend
          if (message.type === 'error') {
            const errorMsg = message.message || 'Unknown error from server'
            console.error('❌ Server error:', errorMsg)

            setError(errorMsg)
            setCurrentStep('error')
            setDebugStats(prev => ({
              ...prev,
              lastError: errorMsg,
              lastErrorTime: new Date()
            }))

            // Stop recording and cleanup
            cleanup()
            setIsRecording(false)
          }

          // Handle other message types (interim_transcript, etc.)
          else if (message.type === 'interim_transcript') {
            console.log('📝 Received interim transcript:', message.data)
          }

        } catch (e) {
          // Not JSON, ignore
          console.log('📨 Non-JSON message:', event.data)
        }
      }
    })
  }, [])
  
  // Step 3: Send audio-start message
  const sendAudioStartMessage = useCallback(async (ws: WebSocket): Promise<void> => {
    console.log('📤 Step 3: Sending audio-start message')

    if (ws.readyState !== WebSocket.OPEN) {
      throw new Error('WebSocket not connected')
    }

    const startMessage = {
      type: 'audio-start',
      data: {
        rate: 16000,
        width: 2,
        channels: 1,
        mode: mode  // Pass recording mode to backend
      },
      payload_length: null
    }

    ws.send(JSON.stringify(startMessage) + '\n')
    console.log('✅ Audio-start message sent with mode:', mode)
  }, [mode])
  
  // Step 4: Start audio streaming
  const startAudioStreaming = useCallback(async (stream: MediaStream, ws: WebSocket): Promise<void> => {
    console.log('🎵 Step 4: Starting audio streaming')

    // Set up audio context and analyser for visualization
    const audioContext = new AudioContext({ sampleRate: 16000 })
    const analyser = audioContext.createAnalyser()
    const source = audioContext.createMediaStreamSource(stream)

    analyser.fftSize = 256
    source.connect(analyser)

    console.log('🎧 Audio context state:', audioContext.state)
    console.log('🎧 Analyser created:', analyser)
    console.log('🎧 Sample rate:', audioContext.sampleRate)

    // Resume audio context if suspended (required by some browsers)
    if (audioContext.state === 'suspended') {
      console.log('🎧 Resuming suspended audio context...')
      await audioContext.resume()
      console.log('🎧 Audio context resumed, new state:', audioContext.state)
    }

    audioContextRef.current = audioContext
    analyserRef.current = analyser

    // Wait brief moment for backend to process audio-start
    await new Promise(resolve => setTimeout(resolve, 100))
    
    // Set up audio processing
    const processor = audioContext.createScriptProcessor(4096, 1, 1)
    source.connect(processor)
    processor.connect(audioContext.destination)

    let processCallCount = 0
    processor.onaudioprocess = (event) => {
      processCallCount++

      // Calculate audio level for first few chunks
      const inputData = event.inputBuffer.getChannelData(0)
      let sum = 0
      for (let i = 0; i < inputData.length; i++) {
        sum += Math.abs(inputData[i])
      }
      const avgLevel = sum / inputData.length

      // Log first few calls to debug
      if (processCallCount <= 3) {
        console.log(`🎵 Audio process callback #${processCallCount}`, {
          wsState: ws?.readyState,
          wsOpen: ws?.readyState === WebSocket.OPEN,
          audioProcessingStarted: audioProcessingStartedRef.current,
          audioLevel: avgLevel.toFixed(6),
          hasAudio: avgLevel > 0.001
        })
      }

      if (!ws || ws.readyState !== WebSocket.OPEN) {
        if (processCallCount === 1) {
          console.warn('⚠️ WebSocket not open in audio callback')
        }
        return
      }

      if (!audioProcessingStartedRef.current) {
        console.log('🚫 Audio processing not started yet, skipping chunk')
        return
      }

      // inputData already declared above for audio level calculation

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
        if (ws.binaryType !== 'arraybuffer') {
          ws.binaryType = 'arraybuffer'
          console.log('🔧 Set WebSocket binaryType to arraybuffer for audio chunks')
        }
        
        ws.send(JSON.stringify(chunkHeader) + '\n')
        ws.send(new Uint8Array(pcmBuffer.buffer, pcmBuffer.byteOffset, pcmBuffer.byteLength))

        // Update debug stats
        chunkCountRef.current++
        setDebugStats(prev => ({ ...prev, chunksSent: chunkCountRef.current }))

        // Log first few chunks
        if (chunkCountRef.current <= 3) {
          console.log(`✅ Sent audio chunk #${chunkCountRef.current}, size: ${pcmBuffer.byteLength} bytes`)
        }
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
    audioProcessingStartedRef.current = true
    
    console.log('✅ Audio streaming started')
  }, [])
  
  // Main start recording function - sequential flow
  const startRecording = useCallback(async () => {
    try {
      setError(null)
      setCurrentStep('mic')
      
      // Step 1: Get microphone access
      const stream = await getMicrophoneAccess()
      
      setCurrentStep('websocket')
      // Step 2: Connect WebSocket (includes stabilization delay)
      const ws = await connectWebSocket()
      
      setCurrentStep('audio-start')
      // Step 3: Send audio-start message
      await sendAudioStartMessage(ws)
      
      setCurrentStep('streaming')
      // Step 4: Start audio streaming (includes processing delay)
      await startAudioStreaming(stream, ws)
      
      // All steps complete - mark as recording
      setIsRecording(true)
      setRecordingDuration(0)
      
      // Start duration timer
      durationIntervalRef.current = setInterval(() => {
        setRecordingDuration(prev => prev + 1)
      }, 1000)
      
      console.log('🎉 Recording started successfully!')
      
    } catch (error) {
      console.error('❌ Recording failed:', error)
      setCurrentStep('error')
      setError(error instanceof Error ? error.message : 'Recording failed')
      setDebugStats(prev => ({ 
        ...prev, 
        lastError: error instanceof Error ? error.message : 'Recording failed',
        lastErrorTime: new Date()
      }))
      cleanup()
    }
  }, [getMicrophoneAccess, connectWebSocket, sendAudioStartMessage, startAudioStreaming, cleanup])
  
  // Stop recording function
  const stopRecording = useCallback(() => {
    if (!isRecording) return
    
    console.log('🛑 Stopping recording')
    setCurrentStep('stopping')
    
    // Stop audio processing
    audioProcessingStartedRef.current = false
    
    // Send audio-stop message if WebSocket is still open
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      try {
        const stopMessage = {
          type: 'audio-stop',
          data: { timestamp: Date.now() },
          payload_length: null
        }
        wsRef.current.send(JSON.stringify(stopMessage) + '\n')
        console.log('📤 Audio-stop message sent')
      } catch (error) {
        console.error('Failed to send audio-stop:', error)
      }
    }
    
    // Cleanup resources
    cleanup()
    
    // Reset state
    setIsRecording(false)
    setRecordingDuration(0)
    setCurrentStep('idle')
    
    console.log('✅ Recording stopped')
  }, [isRecording, cleanup])
  
  // Cleanup on unmount
  useEffect(() => {
    return () => {
      cleanup()
    }
  }, [cleanup])
  
  return {
    currentStep,
    isRecording,
    recordingDuration,
    error,
    mode,
    startRecording,
    stopRecording,
    setMode,
    analyser: analyserRef.current,
    debugStats,
    formatDuration,
    canAccessMicrophone
  }
}