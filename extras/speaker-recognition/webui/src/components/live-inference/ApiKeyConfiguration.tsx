/**
 * Component for API key configuration when manual entry is required
 */

import React from 'react'
import { AlertCircle } from 'lucide-react'

export interface ApiKeyConfigurationProps {
  apiKeySource: 'server' | 'manual' | 'loading'
  apiKey: string
  onApiKeyChange: (key: string) => void
}

export function ApiKeyConfiguration({
  apiKeySource,
  apiKey,
  onApiKeyChange
}: ApiKeyConfigurationProps) {
  // Only show if manual key is needed and not provided
  if (apiKeySource !== 'manual' || apiKey) {
    return null
  }

  return (
    <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
      <div className="flex items-center space-x-2 mb-2">
        <AlertCircle className="h-5 w-5 text-yellow-600" />
        <h3 className="text-sm font-medium text-yellow-800">API Key Required</h3>
      </div>
      <p className="text-sm text-yellow-700 mb-3">
        Server API key not configured. Please enter your Deepgram API key:
      </p>
      <input
        type="password"
        value={apiKey}
        onChange={(e) => onApiKeyChange(e.target.value)}
        placeholder="Enter your Deepgram API key"
        className="w-full px-3 py-2 border border-gray-300 rounded-md"
      />
      <p className="text-xs text-gray-600 mt-1">
        Get your key from{' '}
        <a
          href="https://console.deepgram.com/"
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 underline"
        >
          Deepgram Console
        </a>
      </p>
    </div>
  )
}
