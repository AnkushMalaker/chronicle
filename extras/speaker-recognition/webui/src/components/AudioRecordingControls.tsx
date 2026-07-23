/**
 * AudioRecordingControls Component - Unified recording interface
 * Provides consistent recording UI across Inference, InferLive, and other components
 * Uses the useAudioRecording hook for state management
 */

import React from 'react'
import { Mic, MicOff, Square, AlertCircle, CheckCircle } from 'lucide-react'
import { UseAudioRecordingReturn } from '../hooks/useAudioRecording'
import { formatDuration } from '../utils/audioUtils'

export interface AudioRecordingControlsProps {
  recording: UseAudioRecordingReturn
  disabled?: boolean
  showQuality?: boolean
  compact?: boolean
  className?: string
}

export const AudioRecordingControls: React.FC<AudioRecordingControlsProps> = ({
  recording,
  disabled = false,
  showQuality = true,
  compact = false,
  className = ''
}) => {
  const { recordingState, processedAudio, startRecording, stopRecording, clearRecording } = recording

  const getStatusIcon = () => {
    switch (recordingState.status) {
      case 'starting':
        return <div className="w-4 h-4 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
      case 'recording':
        return <div className="w-3 h-3 bg-red-600 rounded-full animate-pulse" />
      case 'stopping':
        return <div className="w-4 h-4 border-2 border-gray-600 border-t-transparent rounded-full animate-spin" />
      case 'error':
        return <AlertCircle className="h-4 w-4 text-red-600" />
      default:
        return <Mic className="h-4 w-4" />
    }
  }

  const getStatusText = () => {
    switch (recordingState.status) {
      case 'starting':
        return 'Starting...'
      case 'recording':
        return `Recording... ${formatDuration(recordingState.duration * 1000)}`
      case 'stopping':
        return 'Stopping...'
      case 'error':
        return 'Error'
      default:
        return 'Ready to record'
    }
  }

  const getStatusColor = () => {
    switch (recordingState.status) {
      case 'recording':
        return 'text-red-600'
      case 'error':
        return 'text-red-600'
      case 'starting':
      case 'stopping':
        return 'text-blue-600'
      default:
        return 'text-gray-600'
    }
  }

  const getQualityBadge = () => {
    if (!processedAudio?.quality) return null

    const { level, snr } = processedAudio.quality
    const colors: Record<string, string> = {
      excellent: 'bg-green-100 text-green-800',
      good: 'bg-blue-100 text-blue-800',
      fair: 'bg-yellow-100 text-yellow-800',
      poor: 'bg-red-100 text-red-800'
    }

    return (
      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${colors[level] || colors.poor}`}>
        {level.charAt(0).toUpperCase() + level.slice(1)} ({snr.toFixed(1)} dB)
      </span>
    )
  }

  const isRecordingDisabled = disabled || recordingState.status === 'starting' || recordingState.status === 'stopping'

  if (compact) {
    return (
      <div className={`flex items-center space-x-2 ${className}`}>
        {!recordingState.isRecording ? (
          <button
            onClick={startRecording}
            disabled={isRecordingDisabled}
            className="flex items-center space-x-2 px-3 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            title="Start recording"
          >
            {getStatusIcon()}
            <span className="text-sm">{recordingState.isRecording ? 'Recording' : 'Record'}</span>
          </button>
        ) : (
          <button
            onClick={stopRecording}
            className="flex items-center space-x-2 px-3 py-2 bg-gray-600 text-white rounded-md hover:bg-gray-700"
            title="Stop recording"
          >
            <Square className="h-4 w-4" />
            <span className="text-sm">Stop</span>
          </button>
        )}

        {processedAudio && (
          <button
            onClick={clearRecording}
            className="px-2 py-1 text-gray-500 hover:text-gray-700 text-sm"
            title="Clear recording"
          >
            Clear
          </button>
        )}
      </div>
    )
  }

  return (
    <div className={`border rounded-lg p-4 ${className}`}>
      <h4 className="mb-3 heading-sm">🎤 Record Audio</h4>

      {/* Recording Status */}
      <div className="text-center space-y-4">
        {/* Status Indicator */}
        <div className={`flex items-center justify-center space-x-2 font-medium ${getStatusColor()}`}>
          {getStatusIcon()}
          <span className='text-primary'>{getStatusText()}</span>
        </div>

        {/* Recording Controls */}
        <div className="flex justify-center space-x-3">
          {!recordingState.isRecording ? (
            <button
              onClick={startRecording}
              disabled={isRecordingDisabled}
              className="flex items-center space-x-2 px-6 py-3 bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <Mic className="h-5 w-5" />
              <span>Start Recording</span>
            </button>
          ) : (
            <button
              onClick={stopRecording}
              className="flex items-center space-x-2 px-6 py-3 bg-gray-600 text-white rounded-md hover:bg-gray-700 transition-colors"
            >
              <MicOff className="h-5 w-5" />
              <span>Stop Recording</span>
            </button>
          )}

          {processedAudio && !recordingState.isRecording && (
            <button
              onClick={clearRecording}
              className="px-4 py-3 text-gray-600 border border-gray-300 rounded-md hover:bg-gray-50 transition-colors"
            >
              Clear
            </button>
          )}
        </div>

        {/* Browser Compatibility Info */}
        <div className="text-sm text-primary space-y-1">
          <p>Record audio for speaker identification</p>
          <p className="text-xs">
            {location.protocol !== 'https:' && location.hostname !== 'localhost'
              ? '⚠️ HTTPS required for microphone access'
              : '✓ Ready to record'}
          </p>
        </div>

        {/* Error Display */}
        {recordingState.error && (
          <div className="bg-red-50 border border-red-200 rounded-md p-3">
            <div className="flex items-center space-x-2">
              <AlertCircle className="h-4 w-4 text-red-600" />
              <p className="text-sm text-red-800">{recordingState.error}</p>
            </div>
          </div>
        )}

        {/* Processed Audio Info */}
        {processedAudio && (
          <div className="bg-gray-50 rounded-lg p-3 space-y-2">
            <div className="flex items-center justify-center space-x-2 text-green-600">
              <CheckCircle className="h-4 w-4" />
              <span className="text-sm font-medium">Recording Ready</span>
            </div>

            <div className="grid grid-cols-2 gap-4 text-sm">
              <div className="text-center">
                <div className="text-primary">Duration</div>
                <div className="font-medium">{formatDuration(processedAudio.buffer.duration * 1000)}</div>
              </div>
              <div className="text-center">
                <div className="text-primary">Sample Rate</div>
                <div className="font-medium">{(processedAudio.buffer.sampleRate / 1000).toFixed(1)} kHz</div>
              </div>
            </div>

            {/* Quality Badge */}
            {showQuality && processedAudio.quality && (
              <div className="flex justify-center">
                {getQualityBadge()}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default AudioRecordingControls
