import React, { useState, useRef, useCallback, useEffect } from 'react'
import { Mic, MicOff, Square, Play, Pause, Volume2, Settings } from 'lucide-react'

interface AudioCaptureConfig {
  sampleRate: number
  channels: number
  bufferSize: number
  echoCancellation: boolean
  noiseSuppression: boolean
  autoGainControl: boolean
}

interface LiveAudioCaptureProps {
  onAudioData?: (audioBuffer: Float32Array, sampleRate: number) => void
  onStatusChange?: (status: 'idle' | 'requesting' | 'recording' | 'paused' | 'error') => void
  onError?: (error: Error) => void
  config?: Partial<AudioCaptureConfig>
  showWaveform?: boolean
  showControls?: boolean
  recording?: boolean // External control of recording state
  autoControl?: boolean // Hide manual controls when true
}

const DEFAULT_CONFIG: AudioCaptureConfig = {
  sampleRate: 16000,
  channels: 1,
  bufferSize: 4096,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true
}

export default function LiveAudioCapture({
  onAudioData,
  onStatusChange,
  onError,
  config = {},
  showWaveform = true,
  showControls = true,
  recording,
  autoControl = false
}: LiveAudioCaptureProps) {
  const [status, setStatus] = useState<'idle' | 'requesting' | 'recording' | 'paused' | 'error'>('idle')
  const [audioLevel, setAudioLevel] = useState(0)
  const [showSettings, setShowSettings] = useState(false)

  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const animationFrameRef = useRef<number>()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const audioPacketCountRef = useRef<number>(0)

  const fullConfig = { ...DEFAULT_CONFIG, ...config }

  const updateStatus = useCallback((newStatus: typeof status) => {
    setStatus(newStatus)
    onStatusChange?.(newStatus)
  }, [onStatusChange])

  // Handle external recording control
  useEffect(() => {
    if (recording !== undefined && autoControl) {
      console.log('🎤 [EXTERNAL] Recording prop changed:', recording)
      if (recording && status === 'idle') {
        console.log('🎤 [EXTERNAL] Starting recording due to external control')
        startRecording()
      } else if (!recording && (status === 'recording' || status === 'paused')) {
        console.log('🎤 [EXTERNAL] Stopping recording due to external control')
        stopRecording()
      }
    }
  }, [recording, autoControl, status])

  const handleError = useCallback((error: Error) => {
    console.error('Audio capture error:', error)
    updateStatus('error')
    onError?.(error)
  }, [onError, updateStatus])

  const startRecording = async () => {
    console.log('🎤 Starting audio recording...')
    try {
      updateStatus('requesting')

      // Request microphone access
      console.log('🎤 Requesting microphone access...')
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: fullConfig.sampleRate,
          channelCount: fullConfig.channels,
          echoCancellation: fullConfig.echoCancellation,
          noiseSuppression: fullConfig.noiseSuppression,
          autoGainControl: fullConfig.autoGainControl
        }
      })

      console.log('🎤 Microphone access granted, stream:', stream)
      mediaStreamRef.current = stream

      // Create audio context
      console.log('🎤 Creating audio context...')
      const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)({
        sampleRate: fullConfig.sampleRate
      })
      audioContextRef.current = audioContext
      console.log('🎤 Audio context created, sample rate:', audioContext.sampleRate)

      // Create source from stream
      const source = audioContext.createMediaStreamSource(stream)
      sourceRef.current = source

      // Create analyser for visualization
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 256
      analyser.smoothingTimeConstant = 0.8
      analyserRef.current = analyser

      // Create audio worklet processor for audio data
      try {
        // Try to use AudioWorkletNode (modern approach)
        await audioContext.audioWorklet.addModule(
          'data:text/javascript,' + encodeURIComponent(`
            class AudioCaptureProcessor extends AudioWorkletProcessor {
              constructor() {
                super();
                this.packetCount = 0;
                console.log('🔧 [WORKLET] AudioCaptureProcessor constructor called');
              }

              process(inputs, outputs, parameters) {
                try {
                  this.packetCount++;

                  // Debug: Log first few packets
                  if (this.packetCount <= 5) {
                    console.log('🔧 [WORKLET] process() called #' + this.packetCount);
                    console.log('🔧 [WORKLET] inputs.length:', inputs.length);
                    console.log('🔧 [WORKLET] inputs[0]:', inputs[0]);
                  }

                  const input = inputs[0];
                  if (!input) {
                    if (this.packetCount <= 5) {
                      console.warn('🔧 [WORKLET] No input available');
                    }
                    return true;
                  }

                  if (input.length === 0) {
                    if (this.packetCount <= 5) {
                      console.warn('🔧 [WORKLET] Input has no channels');
                    }
                    return true;
                  }

                  const audioData = input[0]; // Get first channel
                  if (!audioData) {
                    if (this.packetCount <= 5) {
                      console.warn('🔧 [WORKLET] First channel is null/undefined');
                    }
                    return true;
                  }

                  // Debug: Log audio characteristics for first few packets
                  if (this.packetCount <= 5) {
                    const maxAmplitude = Math.max(...Array.from(audioData.map(Math.abs)));
                    const rmsLevel = Math.sqrt(audioData.reduce((sum, sample) => sum + sample * sample, 0) / audioData.length);
                    console.log('🔧 [WORKLET] Audio characteristics:', {
                      length: audioData.length,
                      maxAmplitude: maxAmplitude.toFixed(6),
                      rmsLevel: rmsLevel.toFixed(6),
                      firstSample: audioData[0].toFixed(6),
                      lastSample: audioData[audioData.length - 1].toFixed(6)
                    });
                  }

                  // Send audio data via message port
                  this.port.postMessage({
                    type: 'audioData',
                    data: audioData.slice() // Copy the data
                  });

                  return true; // Keep processor alive
                } catch (error) {
                  console.error('🔧 [WORKLET] Error in process():', error);
                  return true; // Keep trying even on error
                }
              }
            }
            registerProcessor('audio-capture-processor', AudioCaptureProcessor);
          `)
        )

        const workletNode = new AudioWorkletNode(audioContext, 'audio-capture-processor')
        processorRef.current = workletNode as any

        workletNode.port.onmessage = (event) => {
          if (event.data.type === 'audioData') {
            audioPacketCountRef.current++

            // When using external control (autoControl=true), prioritize the recording prop
            // Otherwise, use internal status
            const shouldSendAudio = autoControl ? recording : (status === 'recording')

            if (shouldSendAudio) {
              onAudioData?.(event.data.data, audioContext.sampleRate)
            } else {
              // Log why audio is not being sent for debugging
              if (audioPacketCountRef.current <= 3) {
                console.log('🎤 [CAPTURE] Audio captured but not sent - autoControl:', autoControl, 'recording:', recording, 'status:', status)
              }
            }
          }
        }

        // Connect nodes with debugging
        console.log('🔧 [SETUP] Connecting audio graph: MediaStreamSource → Analyser → AudioWorkletNode')
        console.log('🔧 [SETUP] source:', source)
        console.log('🔧 [SETUP] analyser:', analyser)
        console.log('🔧 [SETUP] workletNode:', workletNode)

        source.connect(analyser)
        console.log('🔧 [SETUP] ✅ Connected source to analyser')

        analyser.connect(workletNode)
        console.log('🔧 [SETUP] ✅ Connected analyser to workletNode')

        // Verify audio context state
        console.log('🔧 [SETUP] AudioContext state:', audioContext.state)
        if (audioContext.state === 'suspended') {
          console.log('🔧 [SETUP] Resuming suspended AudioContext...')
          await audioContext.resume()
          console.log('🔧 [SETUP] AudioContext resumed, new state:', audioContext.state)
        }

      } catch (error) {
        console.warn('🔧 [FALLBACK] AudioWorklet not supported, falling back to ScriptProcessorNode:', error)

        // Fallback to ScriptProcessorNode for older browsers
        console.log('🔧 [FALLBACK] Creating ScriptProcessorNode with bufferSize:', fullConfig.bufferSize)
        const processor = audioContext.createScriptProcessor(fullConfig.bufferSize, fullConfig.channels, fullConfig.channels)
        processorRef.current = processor

        processor.onaudioprocess = (event) => {
          // When using external control (autoControl=true), prioritize the recording prop
          // Otherwise, use internal status
          const shouldSendAudio = autoControl ? recording : (status === 'recording')

          if (!shouldSendAudio) {
            // Log why audio is not being sent for debugging
            audioPacketCountRef.current++
            if (audioPacketCountRef.current <= 3) {
              console.log('🔧 [FALLBACK] Audio captured but not sent - autoControl:', autoControl, 'recording:', recording, 'status:', status)
            }
            return
          }

          const inputBuffer = event.inputBuffer
          const audioData = inputBuffer.getChannelData(0) // Get first channel

          audioPacketCountRef.current++

          // Debug: Log audio characteristics for first few packets
          if (audioPacketCountRef.current <= 5) {
            const maxAmplitude = Math.max(...Array.from(audioData.map(Math.abs)))
            const rmsLevel = Math.sqrt(audioData.reduce((sum, sample) => sum + sample * sample, 0) / audioData.length)
            console.log('🔧 [FALLBACK] ScriptProcessor audio #' + audioPacketCountRef.current + ':', {
              length: audioData.length,
              maxAmplitude: maxAmplitude.toFixed(6),
              rmsLevel: rmsLevel.toFixed(6),
              firstSample: audioData[0].toFixed(6),
              lastSample: audioData[audioData.length - 1].toFixed(6)
            })
          }

          // Send audio data to callback
          onAudioData?.(audioData, audioContext.sampleRate)
        }

        // Connect nodes (IMPORTANT: Don't connect to destination to avoid feedback)
        console.log('🔧 [FALLBACK] Connecting audio graph: MediaStreamSource → Analyser → ScriptProcessorNode')
        source.connect(analyser)
        analyser.connect(processor)
        // NOTE: Removed processor.connect(audioContext.destination) to prevent audio feedback
        console.log('🔧 [FALLBACK] ✅ ScriptProcessorNode audio graph connected')
      }

      updateStatus('recording')

      // Reset audio packet counter
      audioPacketCountRef.current = 0

      if (showWaveform) {
        startVisualization()
      }

    } catch (error) {
      handleError(error instanceof Error ? error : new Error('Failed to start recording'))
    }
  }

  const stopRecording = () => {
    console.log('🎤 Stopping audio recording...')
    // Stop visualization
    if (animationFrameRef.current) {
      cancelAnimationFrame(animationFrameRef.current)
    }

    // Disconnect and clean up audio nodes
    if (sourceRef.current) {
      sourceRef.current.disconnect()
      sourceRef.current = null
    }

    if (processorRef.current) {
      processorRef.current.disconnect()
      processorRef.current = null
    }

    if (analyserRef.current) {
      analyserRef.current.disconnect()
      analyserRef.current = null
    }

    // Close audio context
    if (audioContextRef.current) {
      audioContextRef.current.close()
      audioContextRef.current = null
    }

    // Stop media stream
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop())
      mediaStreamRef.current = null
    }

    updateStatus('idle')
    setAudioLevel(0)
  }

  const pauseRecording = () => {
    console.log('🎤 Pausing audio recording...')
    if (audioContextRef.current) {
      audioContextRef.current.suspend()
      updateStatus('paused')
    }
  }

  const resumeRecording = () => {
    console.log('🎤 Resuming audio recording...')
    if (audioContextRef.current) {
      audioContextRef.current.resume()
      updateStatus('recording')
    }
  }

  const startVisualization = () => {
    if (!analyserRef.current || !canvasRef.current) return

    const analyser = analyserRef.current
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const bufferLength = analyser.frequencyBinCount
    const dataArray = new Uint8Array(bufferLength)

    const draw = () => {
      if (status !== 'recording' && status !== 'paused') return

      analyser.getByteFrequencyData(dataArray)

      // Calculate average volume for level meter
      const average = dataArray.reduce((sum, value) => sum + value, 0) / dataArray.length
      setAudioLevel(average / 255)

      // Draw waveform
      ctx.fillStyle = '#f3f4f6'
      ctx.fillRect(0, 0, canvas.width, canvas.height)

      ctx.lineWidth = 2
      ctx.strokeStyle = status === 'recording' ? '#3b82f6' : '#6b7280'
      ctx.beginPath()

      const sliceWidth = canvas.width / bufferLength
      let x = 0

      for (let i = 0; i < bufferLength; i++) {
        const v = dataArray[i] / 128.0
        const y = (v * canvas.height) / 2

        if (i === 0) {
          ctx.moveTo(x, y)
        } else {
          ctx.lineTo(x, y)
        }

        x += sliceWidth
      }

      ctx.stroke()

      animationFrameRef.current = requestAnimationFrame(draw)
    }

    draw()
  }

  useEffect(() => {
    return () => {
      if (status === 'recording' || status === 'paused') {
        stopRecording()
      }
    }
  }, [])

  const getStatusColor = () => {
    switch (status) {
      case 'recording': return 'text-green-600 bg-green-100'
      case 'paused': return 'text-yellow-600 bg-yellow-100'
      case 'error': return 'text-red-600 bg-red-100'
      case 'requesting': return 'text-blue-600 bg-blue-100'
      default: return 'text-gray-600 bg-gray-100'
    }
  }

  const getStatusText = () => {
    switch (status) {
      case 'recording': return 'Recording'
      case 'paused': return 'Paused'
      case 'error': return 'Error'
      case 'requesting': return 'Requesting Access'
      default: return 'Ready'
    }
  }

  return (
    <div className="space-y-4">
      {/* Status and Controls */}
      {showControls && !autoControl && (
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${getStatusColor()}`}>
              {getStatusText()}
            </span>

            {/* Audio Level Indicator */}
            <div className="flex items-center space-x-2">
              <Volume2 className="h-4 w-4 text-gray-500" />
              <div className="w-20 h-2 bg-gray-200 rounded-full overflow-hidden">
                <div
                  className="h-full bg-blue-500 transition-all duration-100"
                  style={{ width: `${audioLevel * 100}%` }}
                />
              </div>
            </div>
          </div>

          <div className="flex items-center space-x-2">
            <button
              onClick={() => setShowSettings(!showSettings)}
              className="p-2 text-gray-600 hover:text-gray-800 border rounded-md"
              title="Audio Settings"
            >
              <Settings className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}

      {/* Settings Panel */}
      {showSettings && (
        <div className="bg-gray-50 border rounded-lg p-4">
          <h4 className="text-sm font-medium text-gray-900 mb-3">Audio Settings</h4>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <label className="block text-gray-700">Sample Rate</label>
              <span className="text-gray-900">{fullConfig.sampleRate} Hz</span>
            </div>
            <div>
              <label className="block text-gray-700">Channels</label>
              <span className="text-gray-900">{fullConfig.channels}</span>
            </div>
            <div>
              <label className="block text-gray-700">Echo Cancellation</label>
              <span className="text-gray-900">{fullConfig.echoCancellation ? 'Enabled' : 'Disabled'}</span>
            </div>
            <div>
              <label className="block text-gray-700">Noise Suppression</label>
              <span className="text-gray-900">{fullConfig.noiseSuppression ? 'Enabled' : 'Disabled'}</span>
            </div>
          </div>
        </div>
      )}

      {/* Waveform Visualization */}
      {showWaveform && (
        <div className="bg-white border rounded-lg p-4">
          <canvas
            ref={canvasRef}
            width={800}
            height={150}
            className="w-full h-32 border rounded"
            style={{ maxWidth: '100%' }}
          />
        </div>
      )}

      {/* Control Buttons */}
      {showControls && !autoControl && (
        <div className="flex justify-center space-x-4">
          {status === 'idle' || status === 'error' ? (
            <button
              onClick={startRecording}
              disabled={status === 'requesting'}
              className="flex items-center space-x-2 px-6 py-3 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Mic className="h-5 w-5" />
              <span>Start Recording</span>
            </button>
          ) : status === 'recording' ? (
            <>
              <button
                onClick={pauseRecording}
                className="flex items-center space-x-2 px-4 py-2 bg-yellow-600 text-white rounded-lg hover:bg-yellow-700"
              >
                <Pause className="h-4 w-4" />
                <span>Pause</span>
              </button>
              <button
                onClick={stopRecording}
                className="flex items-center space-x-2 px-4 py-2 bg-gray-600 text-white rounded-lg hover:bg-gray-700"
              >
                <Square className="h-4 w-4" />
                <span>Stop</span>
              </button>
            </>
          ) : status === 'paused' ? (
            <>
              <button
                onClick={resumeRecording}
                className="flex items-center space-x-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
              >
                <Play className="h-4 w-4" />
                <span>Resume</span>
              </button>
              <button
                onClick={stopRecording}
                className="flex items-center space-x-2 px-4 py-2 bg-gray-600 text-white rounded-lg hover:bg-gray-700"
              >
                <Square className="h-4 w-4" />
                <span>Stop</span>
              </button>
            </>
          ) : null}
        </div>
      )}

      {/* Error Display */}
      {status === 'error' && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <div className="flex items-center space-x-2">
            <MicOff className="h-5 w-5 text-red-600" />
            <div>
              <h4 className="text-red-800 font-medium">Recording Error</h4>
              <p className="text-red-600 text-sm">
                Failed to access microphone. Please check permissions and try again.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
