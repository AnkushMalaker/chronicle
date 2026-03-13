/**
 * Component for recording controls and status display
 */

import React from 'react'
import { Mic } from 'lucide-react'

export type DeepgramStatus = 'disconnected' | 'connecting' | 'connected' | 'error'
export type ApiKeySource = 'server' | 'manual' | 'loading'

export interface RecordingControlsProps {
  isRecording: boolean
  deepgramStatus: DeepgramStatus
  apiKeySource: ApiKeySource
  hasApiKey: boolean
  onToggle: () => void
}

export function RecordingControls({
  isRecording,
  deepgramStatus,
  apiKeySource,
  hasApiKey,
  onToggle
}: RecordingControlsProps) {
  const isDisabled = !hasApiKey || deepgramStatus === 'connecting' || apiKeySource === 'loading'

  const getButtonText = (): string => {
    if (apiKeySource === 'loading') return '⏳ Loading Config...'
    if (deepgramStatus === 'connecting') return '🔄 Connecting...'
    return isRecording ? 'Stop Transcribe & Identify' : 'Start Transcribe & Identify'
  }

  return (
    <>
      {/* Recording Status */}
      {isRecording && (
        <div className="bg-white border rounded-lg p-4">
          <div className="flex items-center space-x-4">
            <div className="flex items-center space-x-2">
              <div className="w-3 h-3 bg-red-600 rounded-full animate-pulse"></div>
              <span className="text-sm font-medium text-gray-900">Recording</span>
            </div>
            <div className="flex items-center space-x-2">
              <Mic className="h-4 w-4 text-gray-500" />
              <span className="text-sm text-gray-600">
                Status: {deepgramStatus === 'connected' ? '✅ Connected' : deepgramStatus}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Control Button */}
      <div className="flex justify-center">
        <button
          onClick={onToggle}
          disabled={isDisabled}
          className={`flex items-center space-x-2 px-8 py-4 rounded-lg font-medium text-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors ${
            isRecording
              ? 'bg-red-600 text-white hover:bg-red-700'
              : 'bg-blue-600 text-white hover:bg-blue-700'
          }`}
        >
          <Mic className="h-6 w-6" />
          <span>{getButtonText()}</span>
        </button>
      </div>
    </>
  )
}
