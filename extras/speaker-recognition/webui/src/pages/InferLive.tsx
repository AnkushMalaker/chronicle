/**
 * InferLive Page - Refactored to use shared components
 * Real-time transcription and speaker identification with dramatically reduced code complexity
 */

import React, { useEffect, useRef } from 'react'
import { Mic, Users, Clock, Volume2 } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { useDeepgramIntegration } from '../hooks/useDeepgramIntegration'
import SpeakerResultsDisplay from '../components/SpeakerResultsDisplay'
import SettingsPanel from '../components/SettingsPanel'

export default function InferLive() {
  const { user } = useUser()

  // Audio processing refs
  const mediaStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)

  // Use our unified Deepgram integration hook
  const deepgram = useDeepgramIntegration({
    userId: user?.id,
    onError: (error) => console.error('Deepgram error:', error),
    onSpeakerIdentified: (result) => {
      console.log('Speaker identified:', result)
    }
  })

  // Setup audio processing when streaming starts
  useEffect(() => {
    if (deepgram.isStreaming && !mediaStreamRef.current) {
      startAudioCapture()
    } else if (!deepgram.isStreaming && mediaStreamRef.current) {
      stopAudioCapture()
    }
  }, [deepgram.isStreaming])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopAudioCapture()
      deepgram.disconnect()
    }
  }, [])

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

      // Get actual sample rate from the audio context
      const actualSampleRate = audioContextRef.current.sampleRate
      console.log(`🎵 [AUDIO] Requested: 16kHz, Actual: ${actualSampleRate}Hz`)

      if (actualSampleRate !== 16000) {
        console.warn(`⚠️ [AUDIO] Sample rate mismatch! Expected 16kHz, got ${actualSampleRate}Hz`)
      }

      // Notify Deepgram hook about the actual sample rate
      deepgram.setActualSampleRate(actualSampleRate)

      const source = audioContextRef.current.createMediaStreamSource(stream)
      const processor = audioContextRef.current.createScriptProcessor(4096, 1, 1)

      processor.onaudioprocess = (event) => {
        if (deepgram.isConnected && deepgram.isStreaming) {
          const inputBuffer = event.inputBuffer
          const inputData = inputBuffer.getChannelData(0)

          // Convert Float32Array to Int16 (linear16 format for Deepgram)
          const int16Buffer = new Int16Array(inputData.length)
          for (let i = 0; i < inputData.length; i++) {
            const sample = Math.max(-1, Math.min(1, inputData[i]))
            int16Buffer[i] = sample * 0x7FFF
          }

          // Send audio to Deepgram (sendAudio will use the actualSampleRate from state)
          deepgram.sendAudio(int16Buffer.buffer)
        }
      }

      source.connect(processor)
      processor.connect(audioContextRef.current.destination)
      processorRef.current = processor

      console.log(`Audio capture started successfully at ${actualSampleRate}Hz`)

    } catch (error) {
      console.error('Failed to start audio capture:', error)

      let errorMessage = 'Failed to access microphone. '
      if (error.name === 'NotAllowedError') {
        errorMessage += 'Please allow microphone access and try again.'
      } else if (error.name === 'NotFoundError') {
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
    if (deepgram.isStreaming) {
      deepgram.stopStreaming()
    } else {
      await deepgram.startStreaming()
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
          <h1 className="heading-lg">🎙️ Live Inference (Complex)</h1>
          <p className="text-secondary">Real-time transcription and speaker identification</p>
          <p className="text-sm text-muted">Direct Deepgram streaming with client-side processing and multiple service coordination</p>
        </div>
      </div>

      {/* Settings Panel */}
      <SettingsPanel
        confidenceThreshold={deepgram.confidenceThreshold}
        onConfidenceThresholdChange={deepgram.setConfidenceThreshold}
        deepgramApiKey={deepgram.apiKey || ''}
        onDeepgramApiKeyChange={deepgram.setApiKey}
        deepgramApiKeySource={deepgram.apiKeySource}
        enableSpeakerIdentification={deepgram.enableSpeakerIdentification}
        onEnableSpeakerIdentificationChange={deepgram.setEnableSpeakerIdentification}
        utteranceEndMs={deepgram.utteranceEndMs}
        onUtteranceEndMsChange={deepgram.setUtteranceEndMs}
        endpointingMs={deepgram.endpointingMs}
        onEndpointingMsChange={deepgram.setEndpointingMs}
        showApiKeySection={true}
        showProcessingOptions={true}
        collapsible={true}
        defaultExpanded={false}
      />

      {/* Session Stats */}
      {deepgram.isStreaming && (
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-6">
              <div className="flex items-center space-x-2">
                <Clock className="h-4 w-4 text-muted" />
                <span className="text-sm text-primary">{formatDuration(deepgram.stats.sessionDuration)}</span>
              </div>
              <div className="flex items-center space-x-2">
                <Volume2 className="h-4 w-4 text-muted" />
                <span className="text-sm text-primary">{deepgram.stats.totalWords} words</span>
              </div>
              <div className="flex items-center space-x-2">
                <Users className="h-4 w-4 text-muted" />
                <span className="text-sm text-primary">{deepgram.stats.identifiedSpeakers.size} speakers</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Connection Error */}
      {deepgram.connectionStatus === 'error' && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 dark:bg-red-900 dark:border-red-800">
          <div className="flex items-center space-x-2">
            <div className="text-red-600 dark:text-red-400">❌</div>
            <div>
              <h4 className="text-red-800 dark:text-red-200 font-medium">Connection Error</h4>
              <p className="text-sm text-red-600 dark:text-red-400">Failed to connect to Deepgram. Please check your API key and internet connection.</p>
            </div>
          </div>
        </div>
      )}

      {/* Recording Status */}
      {deepgram.isStreaming && (
        <div className="card p-4">
          <div className="flex items-center space-x-4">
            <div className="flex items-center space-x-2">
              <div className="w-3 h-3 bg-red-600 rounded-full animate-pulse"></div>
              <span className="text-sm font-medium text-primary">Recording</span>
            </div>
            <div className="flex items-center space-x-2">
              <Mic className="h-4 w-4 text-muted" />
              <span className="text-sm text-secondary">
                Status: {deepgram.connectionStatus === 'connected' ? '✅ Connected' : deepgram.connectionStatus}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Control Button */}
      <div className="flex justify-center">
        <button
          onClick={handleToggleSession}
          disabled={!deepgram.apiKey || deepgram.connectionStatus === 'connecting'}
          className={`flex items-center space-x-2 px-8 py-4 rounded-lg font-medium text-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors ${
            deepgram.isStreaming
              ? 'bg-red-600 text-white hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-800'
              : 'bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800'
          }`}
        >
          <Mic className="h-6 w-6" />
          <span>
            {!deepgram.apiKey ? '⚠️ API Key Required' :
             deepgram.connectionStatus === 'connecting' ? '🔄 Connecting...' :
             deepgram.isStreaming ? 'Stop Transcribe & Identify' :
             'Start Transcribe & Identify'}
          </span>
        </button>
      </div>

      {/* Live Results */}
      <SpeakerResultsDisplay
        liveSegments={deepgram.transcriptSegments}
        showTranscription={true}
        showStats={true}
        showExport={false}
        showAudioPlayback={false}
        compact={false}
        maxHeight="400px"
      />

      {/* Clear Results Button */}
      {deepgram.transcriptSegments.length > 0 && (
        <div className="flex justify-center">
          <button
            onClick={deepgram.clearTranscripts}
            className="px-4 py-2 border rounded-md transition-colors text-secondary hover-bg"
          >
            Clear Transcripts
          </button>
        </div>
      )}
    </div>
  )
}
