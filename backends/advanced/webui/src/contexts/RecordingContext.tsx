import { createContext, useContext, useState, useRef, useCallback, useEffect, useMemo, ReactNode } from 'react'
import { BACKEND_URL } from '../services/api'
import { getStorageKey } from '../utils/storage'
import { useAuth } from './AuthContext'
import { setActiveWakeClientId } from '../hooks/useWakeFeedback'

const log = import.meta.env.DEV ? console.log.bind(console) : () => {}

// Firefox currently ignores `audio: true` in getDisplayMedia — its share picker has
// no "Share audio" option (bugzilla #1541425).
// This is a HINT for UI copy only: never gate capture on it. We always try the picker
// and check whether an audio track actually came back, so the browser decides — a UA
// gate would lock out Chromium forks that sniff as Firefox, and would keep blocking
// Firefox on the day #1541425 ships.
export const likelyLacksDisplayAudio = /firefox/i.test(navigator.userAgent)

// macOS has no built-in loopback input at all: Linux gets PipeWire/PulseAudio
// "Monitor of …" devices for free, but on macOS the user must install a virtual
// audio driver (BlackHole, Loopback, Soundflower) before system audio is capturable.
export const isMacOS = /mac/i.test(navigator.userAgent)

// Inputs that carry system audio rather than a real microphone. Linux names them
// "Monitor of …"; macOS/Windows virtual drivers use their own product names.
export const isLoopbackDevice = (label: string) =>
  /monitor of|loopback|blackhole|soundflower|stereo mix|what ?u ?hear/i.test(label)

export type RecordingStep = 'idle' | 'mic' | 'display-audio' | 'websocket' | 'audio-start' | 'streaming' | 'stopping' | 'error'
export type RecordingMode = 'batch' | 'streaming'
export type AudioSource = 'mic' | 'meeting' | 'tab'

export interface DebugStats {
  chunksSent: number
  messagesReceived: number
  lastError: string | null
  lastErrorTime: Date | null
  sessionStartTime: Date | null
  connectionAttempts: number
}

export interface RecordingContextType {
  // Current state
  currentStep: RecordingStep
  isRecording: boolean
  recordingDuration: number
  error: string | null
  mode: RecordingMode
  liveTranscript: string

  // Actions
  startRecording: () => Promise<void>
  stopRecording: () => void
  setMode: (mode: RecordingMode) => void
  audioSource: AudioSource
  setAudioSource: (source: AudioSource) => void

  // Microphone selection
  availableDevices: MediaDeviceInfo[]
  selectedDeviceId: string | null
  setSelectedDeviceId: (id: string | null) => void

  // System-audio loopback device (Firefox meeting/tab mode)
  monitorDeviceId: string | null
  setMonitorDeviceId: (id: string | null) => void
  requestDeviceAccess: () => Promise<void>

  // What the system-audio capture is actually doing (meeting/tab mode)
  systemAudioLabel: string | null
  systemAudioStatus: 'unknown' | 'active' | 'silent'

  // For components
  analyser: AnalyserNode | null
  debugStats: DebugStats

  // Utilities
  formatDuration: (seconds: number) => string
  canAccessMicrophone: boolean
  likelyLacksDisplayAudio: boolean
}

const RecordingContext = createContext<RecordingContextType | undefined>(undefined)

export function RecordingProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()

  // Basic state
  const [currentStep, setCurrentStep] = useState<RecordingStep>('idle')
  const [isRecording, setIsRecording] = useState(false)
  const [recordingDuration, setRecordingDuration] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<RecordingMode>('streaming')
  const [liveTranscript, setLiveTranscript] = useState('')
  const [analyserState, setAnalyserState] = useState<AnalyserNode | null>(null)
  const [audioSource, setAudioSource] = useState<AudioSource>('mic')

  // Microphone selection
  const [availableDevices, setAvailableDevices] = useState<MediaDeviceInfo[]>([])
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null)
  // System-audio loopback device for Firefox meeting/tab mode ("Monitor of …").
  // Persisted: auto-detect can't know which sink the user actually listens through
  // (each output has its own monitor), so remember the last working choice.
  const [monitorDeviceId, setMonitorDeviceIdState] = useState<string | null>(
    () => localStorage.getItem(getStorageKey('monitorDeviceId'))
  )
  const setMonitorDeviceId = useCallback((id: string | null) => {
    setMonitorDeviceIdState(id)
    if (id) {
      localStorage.setItem(getStorageKey('monitorDeviceId'), id)
    } else {
      localStorage.removeItem(getStorageKey('monitorDeviceId'))
    }
  }, [])
  // Diagnostics for the system-audio capture: which device, and is it delivering signal
  const [systemAudioLabel, setSystemAudioLabel] = useState<string | null>(null)
  const [systemAudioStatus, setSystemAudioStatus] = useState<'unknown' | 'active' | 'silent'>('unknown')

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
  const displayStreamRef = useRef<MediaStream | null>(null)
  const durationIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const keepAliveIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const systemAudioWatchRef = useRef<ReturnType<typeof setInterval>>()
  const chunkCountRef = useRef(0)
  const audioProcessingStartedRef = useRef(false)

  // Check if we're on localhost or using HTTPS
  const isLocalhost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  const isHttps = window.location.protocol === 'https:'

  // DEVELOPMENT ONLY: Allow specific IP addresses (remove in production!)
  const devAllowedHosts = import.meta.env.MODE === 'development'
    ? ['192.168.1.100', '10.0.0.100'] // Add your Docker host IPs here
    : []
  const isDevelopmentHost = devAllowedHosts.includes(window.location.hostname)

  const canAccessMicrophone = isLocalhost || isHttps || isDevelopmentHost

  // Enumerate audio input devices
  const refreshDevices = useCallback(async () => {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices()
      const audioInputs = devices.filter(d => d.kind === 'audioinput')
      setAvailableDevices(audioInputs)
    } catch (e) {
      console.warn('Failed to enumerate audio devices:', e)
    }
  }, [])

  // Device labels are hidden until an audio permission is granted — run a
  // throwaway capture so "Monitor of …" entries become selectable up front.
  const requestDeviceAccess = useCallback(async () => {
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true })
      probe.getTracks().forEach(t => t.stop())
    } catch (e) {
      console.warn('Device access probe failed:', e)
    }
    await refreshDevices()
  }, [refreshDevices])

  // Initial device enumeration + listen for device changes
  useEffect(() => {
    refreshDevices()
    navigator.mediaDevices.addEventListener('devicechange', refreshDevices)
    return () => navigator.mediaDevices.removeEventListener('devicechange', refreshDevices)
  }, [refreshDevices])

  // Format duration helper
  const formatDuration = useCallback((seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }, [])

  // Cleanup function
  const cleanup = useCallback(() => {
    log('Cleaning up audio recording resources')

    // No longer streaming as any client — stop reacting to wake-word SSE feedback.
    setActiveWakeClientId(null)

    // Stop audio processing
    audioProcessingStartedRef.current = false

    // Clean up media stream
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop())
      mediaStreamRef.current = null
    }

    // Clean up display stream (meeting mode)
    if (displayStreamRef.current) {
      displayStreamRef.current.getTracks().forEach(track => track.stop())
      displayStreamRef.current = null
    }

    // Clean up audio context
    if (audioContextRef.current?.state !== 'closed') {
      audioContextRef.current?.close()
    }
    audioContextRef.current = null
    analyserRef.current = null
    setAnalyserState(null)
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

    if (systemAudioWatchRef.current) {
      clearInterval(systemAudioWatchRef.current)
      systemAudioWatchRef.current = undefined
    }
    setSystemAudioLabel(null)
    setSystemAudioStatus('unknown')

    // Reset counters
    chunkCountRef.current = 0
  }, [])

  // Step 1: Get microphone access
  const getMicrophoneAccess = useCallback(async (): Promise<MediaStream> => {
    log('Step 1: Requesting microphone access')

    if (!canAccessMicrophone) {
      throw new Error('Microphone access requires HTTPS or localhost')
    }

    // In meeting mode, disable echo cancellation so speaker/tab audio
    // isn't subtracted from the mic signal
    const disableProcessing = audioSource === 'meeting'
    const audioConstraints: MediaTrackConstraints = {
      channelCount: 1,
      echoCancellation: !disableProcessing,
      noiseSuppression: !disableProcessing,
      autoGainControl: true,
    }
    if (selectedDeviceId) {
      audioConstraints.deviceId = { exact: selectedDeviceId }
    }

    const stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints })

    mediaStreamRef.current = stream

    // Re-enumerate to get labels after permission grant
    refreshDevices()

    // Track when mic permission is revoked
    stream.getTracks().forEach(track => {
      track.onended = () => {
        log('Microphone track ended (permission revoked or device disconnected)')
        if (isRecording) {
          setError('Microphone disconnected or permission revoked')
          setCurrentStep('error')
          cleanup()
          setIsRecording(false)
        }
      }
    })

    log('Microphone access granted')
    return stream
  }, [canAccessMicrophone, selectedDeviceId, isRecording, cleanup, refreshDevices, audioSource])

  // Step 1b: Get display/tab audio (meeting mode only)
  // Returns null (rather than throwing) when the picker yields no audio, so the
  // caller can fall back to a loopback input instead of dead-ending the recording.
  const getDisplayAudio = useCallback(async (): Promise<MediaStream | null> => {
    log('Step 1b: Requesting display/tab audio')

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: true,   // Required for picker to show
        audio: true,   // Request audio track
      })
    } catch (e) {
      // Picker cancelled, or the browser exposes no display capture at all.
      log('getDisplayMedia unavailable or dismissed:', e)
      return null
    }

    // Stop video track — we only need audio. Chrome keeps audio alive.
    stream.getVideoTracks().forEach(t => t.stop())

    // Firefox lands here: it grants the share but silently drops `audio: true`.
    if (stream.getAudioTracks().length === 0) {
      stream.getTracks().forEach(t => t.stop())
      log('Share produced no audio track')
      return null
    }

    displayStreamRef.current = stream
    setSystemAudioLabel(stream.getAudioTracks()[0].label || 'Shared tab audio')

    // Handle user clicking "Stop sharing" in browser chrome
    stream.getAudioTracks()[0].onended = () => {
      log('Display audio track ended (user stopped sharing)')
      displayStreamRef.current = null
    }

    log('Display audio access granted')
    return stream
  }, [])

  // Step 1b (Firefox fallback): capture system audio from a PipeWire/PulseAudio
  // "Monitor of …" loopback input via a second getUserMedia call, since Firefox's
  // getDisplayMedia can't deliver audio. Mixed into the graph exactly like the
  // Chromium display stream. No auto-detect: only the OS knows which output sink
  // audio is routed to, so the user must pick the matching monitor explicitly.
  const getMonitorAudio = useCallback(async (): Promise<MediaStream> => {
    log('Step 1b (Firefox): Capturing system audio via monitor device')

    if (!monitorDeviceId) {
      // Reached only after the share picker already failed to produce audio.
      throw new Error(
        isMacOS
          ? 'No system audio captured. Share a browser tab (not a window or whole screen) with "Share tab audio" ' +
            'ticked — macOS blocks window and screen audio at the OS level. Firefox can\'t share tab audio at all ' +
            '(bugzilla #1541425), so there use a Chromium browser, or install a loopback driver such as BlackHole ' +
            'and pick it in the "System audio" dropdown.'
          : 'No system audio captured. Share a browser tab with "Share tab audio" ticked (window and screen shares ' +
            'never carry audio), or pick the "Monitor of …" entry matching the output you\'re listening through ' +
            'in the "System audio" dropdown.'
      )
    }

    const devices = await navigator.mediaDevices.enumerateDevices()
    const target = devices.find(d => d.kind === 'audioinput' && d.deviceId === monitorDeviceId)
    if (!target) {
      throw new Error(
        'The selected system-audio device is no longer available (output disconnected, or the browser reset ' +
        'device IDs — grant persistent microphone permission to prevent that). Re-select a loopback device ' +
        'in the System audio dropdown.'
      )
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: { exact: target.deviceId },
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    })

    displayStreamRef.current = stream
    setSystemAudioLabel(target.label)
    stream.getAudioTracks()[0].onended = () => {
      log('Monitor audio track ended')
      displayStreamRef.current = null
    }

    log('Monitor audio capture started:', target.label)
    return stream
  }, [monitorDeviceId])

  // Step 2: Connect WebSocket
  const connectWebSocket = useCallback(async (): Promise<WebSocket> => {
    log('Step 2: Connecting to WebSocket')

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
        log('WebSocket connected')

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

          log('WebSocket stabilized and ready')
          resolve(ws)
        }, 100) // 100ms stabilization delay
      }

      ws.onclose = (event) => {
        log('WebSocket disconnected:', event.code, event.reason)
        wsRef.current = null

        if (keepAliveIntervalRef.current) {
          clearInterval(keepAliveIntervalRef.current)
          keepAliveIntervalRef.current = undefined
        }

        // If recording was active, set error state
        if (isRecording) {
          setError('WebSocket connection lost')
          setCurrentStep('error')
          cleanup()
          setIsRecording(false)
        }
      }

      ws.onerror = (error) => {
        console.error('🔌 WebSocket error:', error)
        reject(new Error('Failed to connect to backend'))
      }

      ws.onmessage = (event) => {
        log('Received message from server:', event.data)
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

          // The backend confirms the connection with the resolved client_id. Record it
          // so wake-word SSE feedback can be scoped to this device (see useSSE).
          else if (message.type === 'ready') {
            if (message.client_id) setActiveWakeClientId(message.client_id)
          }

          // Handle other message types (interim_transcript, etc.)
          else if (message.type === 'interim_transcript') {
            log('Received interim transcript:', message.data)
            // Streaming providers send a cumulative transcript that grows over
            // time, so replace (not append) with the latest text.
            const text = message.data?.text
            if (typeof text === 'string' && text.length > 0) {
              setLiveTranscript(text)
            }
          }

        } catch (e) {
          // Not JSON, ignore
          log('Non-JSON message:', event.data)
        }
      }
    })
  }, [isRecording, cleanup])

  // Step 3: Send audio-start message
  const sendAudioStartMessage = useCallback(async (ws: WebSocket): Promise<void> => {
    log('Step 3: Sending audio-start message')

    if (ws.readyState !== WebSocket.OPEN) {
      throw new Error('WebSocket not connected')
    }

    const rate = audioContextRef.current?.sampleRate ?? 16000

    const startMessage = {
      type: 'audio-start',
      data: {
        rate,
        width: 2,
        channels: 1,
        mode: mode  // Pass recording mode to backend
      },
      payload_length: null
    }

    ws.send(JSON.stringify(startMessage) + '\n')
    log(`Audio-start message sent with mode: ${mode}, rate: ${rate}`)
  }, [mode])

  // Step 4: Start audio streaming
  const startAudioStreaming = useCallback(async (micStream: MediaStream | null, ws: WebSocket): Promise<void> => {
    log('Step 4: Starting audio streaming')

    // Reuse the AudioContext created in startRecording
    const audioContext = audioContextRef.current!
    const analyser = audioContext.createAnalyser()
    analyser.fftSize = 256

    log('Audio context state:', audioContext.state, 'Sample rate:', audioContext.sampleRate)

    // Resume audio context if suspended (required by some browsers)
    if (audioContext.state === 'suspended') {
      log('Resuming suspended audio context...')
      await audioContext.resume()
      log('Audio context resumed, new state:', audioContext.state)
    }
    analyserRef.current = analyser
    setAnalyserState(analyser)

    // Wait brief moment for backend to process audio-start
    await new Promise(resolve => setTimeout(resolve, 100))

    // Set up audio processing
    const processor = audioContext.createScriptProcessor(4096, 1, 1)

    // Connect mic source if available
    if (micStream) {
      const micSource = audioContext.createMediaStreamSource(micStream)
      micSource.connect(analyser)
      micSource.connect(processor)
    }

    // Mix in display/tab audio if available
    // NOTE: We do NOT connect display audio to audioContext.destination —
    // the source tab already plays the audio, so replaying it here would cause echo.
    if (displayStreamRef.current && displayStreamRef.current.getAudioTracks().length > 0) {
      const displaySource = audioContext.createMediaStreamSource(displayStreamRef.current)
      displaySource.connect(processor)
      // Use display audio for visualization when no mic
      if (!micStream) {
        displaySource.connect(analyser)
      }
      log('Display audio connected to recording pipeline')

      // Watch the system-audio level so a silent capture (wrong monitor device,
      // idle output sink) is surfaced in the UI instead of discovered post-hoc.
      const sysAnalyser = audioContext.createAnalyser()
      sysAnalyser.fftSize = 2048
      displaySource.connect(sysAnalyser)
      const levelBuf = new Float32Array(sysAnalyser.fftSize)
      let silentTicks = 0
      systemAudioWatchRef.current = setInterval(() => {
        sysAnalyser.getFloatTimeDomainData(levelBuf)
        let peak = 0
        for (let i = 0; i < levelBuf.length; i++) {
          const v = Math.abs(levelBuf[i])
          if (v > peak) peak = v
        }
        if (peak > 0.003) {
          silentTicks = 0
          setSystemAudioStatus('active')
        } else if (++silentTicks >= 4) {
          setSystemAudioStatus('silent')
        }
      }, 1000)
    }

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
        log(`Audio process callback #${processCallCount}`, {
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
        log('Audio processing not started yet, skipping chunk')
        return
      }

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
            rate: audioContext.sampleRate,
            width: 2,
            channels: 1
          },
          payload_length: pcmBuffer.byteLength
        }

        // Set binary type for WebSocket before sending binary data
        if (ws.binaryType !== 'arraybuffer') {
          ws.binaryType = 'arraybuffer'
          log('Set WebSocket binaryType to arraybuffer for audio chunks')
        }

        ws.send(JSON.stringify(chunkHeader) + '\n')
        ws.send(new Uint8Array(pcmBuffer.buffer, pcmBuffer.byteOffset, pcmBuffer.byteLength))

        // Update debug stats
        chunkCountRef.current++
        setDebugStats(prev => ({ ...prev, chunksSent: chunkCountRef.current }))

        // Log first few chunks
        if (chunkCountRef.current <= 3) {
          log(`Sent audio chunk #${chunkCountRef.current}, size: ${pcmBuffer.byteLength} bytes`)
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

    log('Audio streaming started')
  }, [])

  // Main start recording function - sequential flow
  const startRecording = useCallback(async () => {
    const needsMic = audioSource !== 'tab'
    const needsDisplayAudio = audioSource !== 'mic'

    try {
      setError(null)
      setLiveTranscript('')

      // Step 1: Get microphone access (skip for tab-only)
      let micStream: MediaStream | null = null
      if (needsMic) {
        setCurrentStep('mic')
        micStream = await getMicrophoneAccess()
      }

      // Create AudioContext at 16kHz to match the backend pipeline expectation.
      // The browser will internally resample from the mic's native rate (e.g. 48kHz).
      const audioContext = new AudioContext({ sampleRate: 16000 })
      audioContextRef.current = audioContext
      log(`AudioContext created, sample rate: ${audioContext.sampleRate}Hz`)

      // Step 1b: Get display/tab audio if needed. Try the share picker first and
      // fall back to a loopback input only if it produced no audio track — feature
      // detection, not UA sniffing, so Chromium keeps its one-click tab share.
      // Skip the picker outright when the user has already chosen a loopback device.
      if (needsDisplayAudio) {
        setCurrentStep('display-audio')
        const shared = monitorDeviceId ? null : await getDisplayAudio()
        if (!shared) {
          await getMonitorAudio()
        }
      }

      setCurrentStep('websocket')
      // Step 2: Connect WebSocket (includes stabilization delay)
      const ws = await connectWebSocket()

      setCurrentStep('audio-start')
      // Step 3: Send audio-start message (uses audioContextRef for sample rate)
      await sendAudioStartMessage(ws)

      setCurrentStep('streaming')
      // Step 4: Start audio streaming (reuses existing AudioContext)
      await startAudioStreaming(micStream, ws)

      // All steps complete - mark as recording
      setIsRecording(true)
      setRecordingDuration(0)

      // Start duration timer
      durationIntervalRef.current = setInterval(() => {
        setRecordingDuration(prev => prev + 1)
      }, 1000)

      log('Recording started successfully!')

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
  }, [getMicrophoneAccess, getDisplayAudio, getMonitorAudio, monitorDeviceId, audioSource, connectWebSocket, sendAudioStartMessage, startAudioStreaming, cleanup])

  // Stop recording function
  const stopRecording = useCallback(() => {
    if (!isRecording) return

    log('Stopping recording')
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
        log('Audio-stop message sent')
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

    log('Recording stopped')
  }, [isRecording, cleanup])

  // Stop recording when user logs out
  useEffect(() => {
    if (!user && isRecording) {
      log('User logged out, stopping recording')
      stopRecording()
    }
  }, [user, isRecording, stopRecording])

  // Warn user before closing tab during recording
  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (isRecording) {
        event.preventDefault()
        event.returnValue = 'Recording in progress. Are you sure you want to leave?'
        return event.returnValue
      }
    }

    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [isRecording])

  // NOTE: No cleanup on unmount - recording persists across navigation
  // This is intentional for the global recording feature

  const contextValue = useMemo<RecordingContextType>(() => ({
    currentStep,
    isRecording,
    recordingDuration,
    error,
    mode,
    liveTranscript,
    startRecording,
    stopRecording,
    setMode,
    audioSource,
    setAudioSource,
    availableDevices,
    selectedDeviceId,
    setSelectedDeviceId,
    monitorDeviceId,
    setMonitorDeviceId,
    requestDeviceAccess,
    systemAudioLabel,
    systemAudioStatus,
    analyser: analyserState,
    debugStats,
    formatDuration,
    canAccessMicrophone,
    likelyLacksDisplayAudio
  }), [
    currentStep, isRecording, recordingDuration, error, mode, liveTranscript,
    startRecording, stopRecording, setMode,
    audioSource, setAudioSource,
    availableDevices, selectedDeviceId, setSelectedDeviceId,
    monitorDeviceId, setMonitorDeviceId, requestDeviceAccess, systemAudioLabel, systemAudioStatus,
    analyserState, debugStats, formatDuration, canAccessMicrophone
  ])

  return (
    <RecordingContext.Provider value={contextValue}>
      {children}
    </RecordingContext.Provider>
  )
}

export function useRecording() {
  const context = useContext(RecordingContext)
  if (context === undefined) {
    throw new Error('useRecording must be used within a RecordingProvider')
  }
  return context
}
