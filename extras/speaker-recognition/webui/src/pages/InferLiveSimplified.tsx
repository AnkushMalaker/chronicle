/**
 * InferLive Page - Simplified version using Speaker WebSocket
 * Real-time transcription with speaker change detection
 * Much simpler than the original - server handles all complexity
 */

import { useEffect, useRef, useState } from 'react'
import { Mic, Users, Clock, Volume2, Wifi, WifiOff } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { useSpeakerWebSocket } from '../hooks/useSpeakerWebSocket'
import { useDeepgramSession } from '../hooks/useDeepgramSession'
import SettingsPanel from '../components/SettingsPanel'

export default function InferLiveSimplified() {
  const { user } = useUser()

  // Audio processing refs
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)

  // Refs to track real-time state for audio processor (avoids closure capture issues)
  const isConnectedRef = useRef(false)
  const isStreamingRef = useRef(false)

  // Use Deepgram session for API key management
  const deepgramSession = useDeepgramSession()

  // WebSocket settings
  const [confidenceThreshold, setConfidenceThreshold] = useState<number>(0.15)
  const [enableSpeakerIdentification, setEnableSpeakerIdentification] = useState<boolean>(true)

  // Use our simplified WebSocket hook
  const speakerWS = useSpeakerWebSocket({
    userId: user?.id,
    confidenceThreshold,
    deepgramApiKey: deepgramSession.deepgramApiKey || undefined,
  })

  // Keep refs updated with current state (for audio processor real-time access)
  useEffect(() => {
    isConnectedRef.current = speakerWS.isConnected
    isStreamingRef.current = speakerWS.isStreaming
  }, [speakerWS.isConnected, speakerWS.isStreaming])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopAudioCapture()
      speakerWS.disconnect()
    }
  }, [])

  // Update WebSocket settings when they change (but avoid unnecessary updates)
  useEffect(() => {
    // Only update if we have a user and WebSocket is initialized
    if (user?.id && deepgramSession.deepgramApiKey) {
      console.log('🔧 [Settings] Updating WebSocket settings:', {
        userId: user.id,
        confidenceThreshold,
        hasApiKey: !!deepgramSession.deepgramApiKey,
        isConnected: speakerWS.isConnected,
        isStreaming: speakerWS.isStreaming
      })
      speakerWS.updateSettings({
        userId: user?.id,
        confidenceThreshold,
        deepgramApiKey: deepgramSession.deepgramApiKey || undefined,
      })
    }
  }, [user?.id, confidenceThreshold, deepgramSession.deepgramApiKey])

  const startAudioCapture = async () => {
    try {
      // Request microphone access
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

      // Create audio context for processing
      audioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)({
        sampleRate: 16000
      })

      const actualSampleRate = audioContextRef.current.sampleRate
      console.log(`🎵 [AUDIO] Requested: 16kHz, Actual: ${actualSampleRate}Hz`)

      const source = audioContextRef.current.createMediaStreamSource(stream)
      const processor = audioContextRef.current.createScriptProcessor(4096, 1, 1)

      processor.onaudioprocess = (event) => {
        // Use refs for real-time state (avoids React closure capture issues)
        const shouldSend = isConnectedRef.current && isStreamingRef.current

        if (shouldSend) {
          const inputBuffer = event.inputBuffer
          const inputData = inputBuffer.getChannelData(0)

          // Convert Float32Array to Int16 (linear16 format)
          const int16Buffer = new Int16Array(inputData.length)
          for (let i = 0; i < inputData.length; i++) {
            const sample = Math.max(-1, Math.min(1, inputData[i]))
            int16Buffer[i] = sample * 0x7FFF
          }

          // Send audio to WebSocket
          const success = speakerWS.sendAudio(int16Buffer.buffer)
          if (success) {
            // Only log successful sends to reduce spam
            if (Math.random() < 0.01) { // Log ~1% of sends
              console.log(`🎙️ [AUDIO] Sending ${int16Buffer.buffer.byteLength} bytes to WebSocket`)
            }
          } else {
            console.warn('❌ [AUDIO] Failed to send audio data')
          }
        } else if (Math.random() < 0.1) { // Log ~10% of failed attempts to reduce spam
          console.warn(`⚠️ [AUDIO] Not sending audio - connected: ${isConnectedRef.current}, streaming: ${isStreamingRef.current}`)
        }
      }

      source.connect(processor)
      processor.connect(audioContextRef.current.destination)
      processorRef.current = processor

      console.log(`Audio capture started successfully at ${actualSampleRate}Hz`)

    } catch (error) {
      console.error('Failed to start audio capture:', error)

      const err = error as DOMException
      let errorMessage = 'Failed to access microphone. '
      if (err.name === 'NotAllowedError') {
        errorMessage += 'Please allow microphone access and try again.'
      } else if (err.name === 'NotFoundError') {
        errorMessage += 'No microphone found. Please check your device.'
      } else {
        errorMessage += 'Please check permissions and try again.'
      }

      alert(errorMessage)
    }
  }

  const stopAudioCapture = () => {
    // Stop audio processing
    if (processorRef.current) {
      processorRef.current.disconnect()
      processorRef.current = null
    }

    if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
      audioContextRef.current.close()
      audioContextRef.current = null
    }

    // Stop media stream
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop())
      mediaStreamRef.current = null
    }

    console.log('Audio capture stopped')
  }

  const handleToggleSession = async () => {
    if (speakerWS.isStreaming) {
      speakerWS.stopStreaming()
      stopAudioCapture()
    } else {
      try {
        // Connect to WebSocket if not connected
        if (!speakerWS.isConnected) {
          await speakerWS.connect()
        }

        // Start streaming and audio capture
        speakerWS.startStreaming()
        await startAudioCapture()
      } catch (error) {
        console.error('Failed to start session:', error)
        alert('Failed to start session. Please check your settings and try again.')
      }
    }
  }

  const formatDuration = (ms: number): string => {
    const seconds = Math.floor(ms / 1000)
    const minutes = Math.floor(seconds / 60)
    const hours = Math.floor(minutes / 60)

    if (hours > 0) {
      return `${hours}:${(minutes % 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
    }
    return `${minutes}:${(seconds % 60).toString().padStart(2, '0')}`
  }

  const getConnectionStatusIcon = () => {
    switch (speakerWS.connectionStatus) {
      case 'connected':
        return <Wifi className="h-4 w-4 text-green-600 dark:text-green-400" />
      case 'connecting':
        return <Wifi className="h-4 w-4 text-yellow-600 dark:text-yellow-400 animate-pulse" />
      case 'error':
        return <WifiOff className="h-4 w-4 text-red-600 dark:text-red-400" />
      default:
        return <WifiOff className="h-4 w-4 text-gray-400 dark:text-gray-500" />
    }
  }

  const getConnectionStatusText = () => {
    switch (speakerWS.connectionStatus) {
      case 'connected':
        return '✅ Connected'
      case 'connecting':
        return '🔄 Connecting...'
      case 'error':
        return '❌ Error'
      default:
        return '⚪ Disconnected'
    }
  }

  if (!user) {
    return (
      <div className="text-center py-12">
        <Users className="h-16 w-16 text-muted mx-auto mb-4" />
        <h3 className="heading-sm mb-2">User Required</h3>
        <p className="text-muted">Please select a user to access live inference.</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="heading-lg">🎙️ Live Inference</h1>
          <p className="text-secondary">Real-time transcription with server-side processing</p>
          <p className="text-sm text-muted">Server handles audio processing, VAD, and speaker identification</p>
        </div>
      </div>

      {/* Settings Panel */}
      <SettingsPanel
        confidenceThreshold={confidenceThreshold}
        onConfidenceThresholdChange={setConfidenceThreshold}
        deepgramApiKey={deepgramSession.deepgramApiKey}
        onDeepgramApiKeyChange={deepgramSession.setDeepgramApiKey}
        deepgramApiKeySource={deepgramSession.apiKeySource}
        enableSpeakerIdentification={enableSpeakerIdentification}
        onEnableSpeakerIdentificationChange={setEnableSpeakerIdentification}
        showApiKeySection={true}
        showProcessingOptions={true}
        collapsible={true}
        defaultExpanded={false}
      />

      {/* Connection Status */}
      <div className="card p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <div className="flex items-center space-x-2">
              {getConnectionStatusIcon()}
              <span className="text-sm text-primary">
                Status: {getConnectionStatusText()}
              </span>
            </div>
            {speakerWS.isStreaming && (
              <div className="flex items-center space-x-2">
                <div className="w-3 h-3 bg-red-600 rounded-full animate-pulse"></div>
                <span className="text-sm font-medium text-primary">Recording</span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Session Stats */}
      {speakerWS.isStreaming && (
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-6">
              <div className="flex items-center space-x-2">
                <Clock className="h-4 w-4 text-muted" />
                <span className="text-sm text-primary">
                  {formatDuration(speakerWS.stats.sessionDuration)}
                </span>
              </div>
              <div className="flex items-center space-x-2">
                <Volume2 className="h-4 w-4 text-muted" />
                <span className="text-sm text-primary">
                  {speakerWS.stats.totalSegments} segments
                </span>
              </div>
              <div className="flex items-center space-x-2">
                <Users className="h-4 w-4 text-muted" />
                <span className="text-sm text-primary">
                  {speakerWS.stats.identifiedSpeakers.size} speakers
                </span>
              </div>
              {speakerWS.stats.averageConfidence > 0 && (
                <div className="flex items-center space-x-2">
                  <span className="text-sm text-muted">Avg Confidence:</span>
                  <span className="text-sm text-primary">
                    {(speakerWS.stats.averageConfidence * 100).toFixed(1)}%
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Control Button */}
      <div className="flex justify-center">
        <button
          onClick={handleToggleSession}
          disabled={!deepgramSession.deepgramApiKey || speakerWS.connectionStatus === 'connecting'}
          className={`flex items-center space-x-2 px-8 py-4 rounded-lg font-medium text-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors ${
            speakerWS.isStreaming
              ? 'bg-red-600 text-white hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-800'
              : 'bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800'
          }`}
        >
          <Mic className="h-6 w-6" />
          <span>
            {!deepgramSession.deepgramApiKey ? '⚠️ API Key Required' :
             speakerWS.connectionStatus === 'connecting' ? '🔄 Connecting...' :
             speakerWS.isStreaming ? 'Stop Transcribe & Identify' :
             'Start Transcribe & Identify'}
          </span>
        </button>
      </div>

      {/* Live Results */}
      <div className="card">
        <div className="p-4 border-b dark:border-gray-700">
          <h3 className="heading-sm">Live Transcription</h3>
          <p className="text-sm text-muted">Utterance boundaries detected using server-side VAD processing</p>
        </div>
        <div className="p-4 space-y-4 max-h-96 overflow-y-auto">
          {speakerWS.transcriptSegments.length === 0 ? (
            <div className="text-center py-8 text-muted">
              {speakerWS.isStreaming ? 'Listening for speech...' : 'Start streaming to see transcription results'}
            </div>
          ) : (
            speakerWS.transcriptSegments.map((segment) => (
              <div key={segment.id} className={`border-l-4 pl-4 ${
                segment.status === 'interim' ? 'border-yellow-400 opacity-70 dark:border-yellow-500' : 'border-blue-500 dark:border-blue-400'
              }`}>
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="text-sm text-muted mb-1">
                      <span>
                        {segment.speaker_name || 'Unknown Speaker'}
                      </span>
                      {segment.status === 'interim' && (
                        <span className="text-xs bg-yellow-100 text-yellow-800 px-2 py-0.5 rounded-full dark:bg-yellow-900 dark:text-yellow-200 animate-pulse">
                          Speaking...
                        </span>
                      )}
                      {segment.status === 'identified' && segment.confidence > 0 && (
                        <span className="text-xs bg-green-100 text-green-800 px-2 py-0.5 rounded-full dark:bg-green-900 dark:text-green-200">
                          {(segment.confidence * 100).toFixed(1)}%
                        </span>
                      )}
                      {segment.duration > 0 && (
                        <span className="text-xs text-muted">
                          {segment.duration.toFixed(1)}s
                        </span>
                      )}
                    </div>
                    <p className={`${segment.status === 'interim' ? 'text-secondary italic' : 'text-primary'}`}>
                      {segment.text}
                    </p>
                  </div>
                  <div className="text-xs text-muted ml-4">
                    {new Date(segment.timestamp).toLocaleTimeString()}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Clear Results Button */}
      {speakerWS.transcriptSegments.length > 0 && (
        <div className="flex justify-center">
          <button
            onClick={speakerWS.clearTranscripts}
            className="px-4 py-2 border rounded-md transition-colors text-secondary hover-bg"
          >
            Clear Transcripts
          </button>
        </div>
      )}
    </div>
  )
}
