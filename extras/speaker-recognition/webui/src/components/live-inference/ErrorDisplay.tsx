/**
 * Component for displaying session errors with helpful tips
 */

import React from 'react'
import { AlertCircle } from 'lucide-react'

export interface ErrorDisplayProps {
  error: string | null
  onClear: () => void
}

export function ErrorDisplay({ error, onClear }: ErrorDisplayProps) {
  if (!error) {
    return null
  }

  // Helper function to get error-specific tips
  const getErrorTip = (errorMessage: string): string | null => {
    if (errorMessage.includes('API key')) {
      return '💡 Make sure your Deepgram API key is valid and has credits available'
    }
    if (errorMessage.includes('WebSocket')) {
      return '💡 Check your internet connection and firewall settings'
    }
    if (errorMessage.includes('timeout')) {
      return '💡 Connection timed out - try again or check Deepgram service status'
    }
    return null
  }

  const tip = getErrorTip(error)

  return (
    <div className="bg-red-50 border border-red-200 rounded-lg p-4">
      <div className="flex items-center space-x-2">
        <AlertCircle className="h-5 w-5 text-red-600" />
        <div className="flex-1">
          <h4 className="text-red-800 font-medium">Session Error</h4>
          <p className="text-red-600 text-sm">{error}</p>
          {tip && (
            <p className="text-red-500 text-xs mt-1">{tip}</p>
          )}
        </div>
        <button
          onClick={() => {
            console.log('🎙️ [ERROR] Clear error button clicked')
            onClear()
          }}
          className="text-red-400 hover:text-red-600 text-sm"
          title="Clear error"
        >
          ✕
        </button>
      </div>
    </div>
  )
}
