/**
 * SettingsPanel Component - Unified settings interface
 * Provides consistent settings UI across all processing pages
 * Handles confidence thresholds, API keys, and processing options
 */

import React, { useState } from 'react'
import { Settings, Eye, EyeOff, Key, Volume2, Users, AlertCircle, Info } from 'lucide-react'

export interface SettingsPanelProps {
  // Confidence threshold
  confidenceThreshold: number
  onConfidenceThresholdChange: (threshold: number) => void

  // API key management
  deepgramApiKey?: string
  onDeepgramApiKeyChange?: (key: string) => void
  deepgramApiKeySource?: 'server' | 'manual' | 'loading'

  // Processing options
  enableSpeakerIdentification?: boolean
  onEnableSpeakerIdentificationChange?: (enabled: boolean) => void

  enableTranscription?: boolean
  onEnableTranscriptionChange?: (enabled: boolean) => void

  // Audio settings
  sampleRate?: number
  onSampleRateChange?: (rate: number) => void

  // Timing settings
  utteranceEndMs?: number
  onUtteranceEndMsChange?: (ms: number) => void
  endpointingMs?: number
  onEndpointingMsChange?: (ms: number) => void

  // Display options
  compact?: boolean
  collapsible?: boolean
  defaultExpanded?: boolean
  showApiKeySection?: boolean
  showAudioSettings?: boolean
  showProcessingOptions?: boolean

  // Styling
  className?: string
}

export const SettingsPanel: React.FC<SettingsPanelProps> = ({
  confidenceThreshold,
  onConfidenceThresholdChange,
  deepgramApiKey = '',
  onDeepgramApiKeyChange,
  deepgramApiKeySource = 'loading',
  enableSpeakerIdentification = true,
  onEnableSpeakerIdentificationChange,
  enableTranscription = true,
  onEnableTranscriptionChange,
  sampleRate = 16000,
  onSampleRateChange,
  utteranceEndMs = 1000,
  onUtteranceEndMsChange,
  endpointingMs = 300,
  onEndpointingMsChange,
  compact = false,
  collapsible = false,
  defaultExpanded = true,
  showApiKeySection = true,
  showAudioSettings = false,
  showProcessingOptions = true,
  className = ''
}) => {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded)
  const [showApiKey, setShowApiKey] = useState(false)

  const getConfidenceDescription = (threshold: number): string => {
    if (threshold >= 0.8) return 'Very strict - only high confidence matches'
    if (threshold >= 0.6) return 'Strict - good quality matches'
    if (threshold >= 0.4) return 'Moderate - balance of accuracy and coverage'
    if (threshold >= 0.2) return 'Lenient - includes uncertain matches'
    return 'Very lenient - includes all possible matches'
  }

  const getApiKeyStatusColor = () => {
    switch (deepgramApiKeySource) {
      case 'server':
        return 'text-green-600 bg-green-100'
      case 'manual':
        return deepgramApiKey ? 'text-blue-600 bg-blue-100' : 'text-red-600 bg-red-100'
      case 'loading':
        return 'text-gray-600 bg-gray-100'
      default:
        return 'text-gray-600 bg-gray-100'
    }
  }

  const getApiKeyStatusText = () => {
    switch (deepgramApiKeySource) {
      case 'server':
        return '✅ Server configured'
      case 'manual':
        return deepgramApiKey ? '✅ Manual key provided' : '❌ Key required'
      case 'loading':
        return '⏳ Loading...'
      default:
        return 'Unknown'
    }
  }

  const renderHeader = () => (
    <div
      className={`flex items-center justify-between ${collapsible ? 'cursor-pointer' : ''}`}
      onClick={collapsible ? () => setIsExpanded(!isExpanded) : undefined}
    >
      <div className="flex items-center space-x-2">
        <Settings className="h-4 w-4 text-gray-600" />
        <h4 className="font-medium text-gray-900">Processing Settings</h4>
      </div>
      {collapsible && (
        <button
          className="text-gray-400 hover:text-gray-600"
          aria-label={isExpanded ? 'Collapse settings' : 'Expand settings'}
        >
          {isExpanded ? '▼' : '▶'}
        </button>
      )}
    </div>
  )

  const renderContent = () => (
    <div className="space-y-4">
      {/* Confidence Threshold */}
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-2">
          Confidence Threshold: {confidenceThreshold.toFixed(2)}
        </label>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={confidenceThreshold}
          onChange={(e) => onConfidenceThresholdChange(parseFloat(e.target.value))}
          className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer"
        />
        <div className="flex justify-between text-xs text-gray-500 mt-1">
          <span>0.0</span>
          <span>0.5</span>
          <span>1.0</span>
        </div>
        <p className="text-xs text-gray-600 mt-1">
          {getConfidenceDescription(confidenceThreshold)}
        </p>
      </div>

      {/* API Key Section */}
      {showApiKeySection && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-sm font-medium text-gray-700">
              Deepgram API Key
            </label>
            <span className={`px-2 py-1 rounded-full text-xs font-medium ${getApiKeyStatusColor()}`}>
              {getApiKeyStatusText()}
            </span>
          </div>

          {deepgramApiKeySource === 'manual' && (
            <div className="space-y-2">
              <div className="relative">
                <input
                  type={showApiKey ? 'text' : 'password'}
                  value={deepgramApiKey}
                  onChange={(e) => onDeepgramApiKeyChange?.(e.target.value)}
                  placeholder="Enter your Deepgram API key"
                  className="w-full px-3 py-2 pr-10 border border-gray-300 rounded-md text-sm"
                />
                <button
                  type="button"
                  onClick={() => setShowApiKey(!showApiKey)}
                  className="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600"
                >
                  {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
              <p className="text-xs text-gray-600">
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
          )}

          {deepgramApiKeySource === 'server' && (
            <div className="flex items-center space-x-2 text-sm text-gray-600">
              <Key className="h-4 w-4" />
              <span>Using server-configured API key</span>
            </div>
          )}
        </div>
      )}

      {/* Processing Options */}
      {showProcessingOptions && (
        <div className="space-y-3">
          <h5 className="text-sm font-medium text-gray-700">Processing Options</h5>

          {/* Speaker Identification Toggle */}
          {onEnableSpeakerIdentificationChange && (
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Users className="h-4 w-4 text-gray-500" />
                <span className="text-sm text-gray-700">Speaker Identification</span>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableSpeakerIdentification}
                  onChange={(e) => onEnableSpeakerIdentificationChange(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600"></div>
              </label>
            </div>
          )}

          {/* Transcription Toggle */}
          {onEnableTranscriptionChange && (
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Volume2 className="h-4 w-4 text-gray-500" />
                <span className="text-sm text-gray-700">Transcription</span>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableTranscription}
                  onChange={(e) => onEnableTranscriptionChange(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-blue-600"></div>
              </label>
            </div>
          )}
        </div>
      )}

      {/* Timing Settings */}
      {(onUtteranceEndMsChange || onEndpointingMsChange) && (
        <div className="space-y-3">
          <h5 className="text-sm font-medium text-gray-700">Completion Timing</h5>

          {/* Utterance End Timeout */}
          {onUtteranceEndMsChange && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Utterance End Timeout: {utteranceEndMs}ms
              </label>
              <input
                type="range"
                min="500"
                max="5000"
                step="100"
                value={utteranceEndMs}
                onChange={(e) => onUtteranceEndMsChange(parseInt(e.target.value))}
                className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer"
              />
              <div className="flex justify-between text-xs text-gray-500 mt-1">
                <span>0.5s</span>
                <span>2.5s</span>
                <span>5.0s</span>
              </div>
              <p className="text-xs text-gray-600 mt-1">
                How long to wait after silence before completing an utterance
              </p>
            </div>
          )}

          {/* Endpointing Timeout */}
          {onEndpointingMsChange && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Endpointing Timeout: {endpointingMs}ms
              </label>
              <input
                type="range"
                min="100"
                max="1000"
                step="50"
                value={endpointingMs}
                onChange={(e) => onEndpointingMsChange(parseInt(e.target.value))}
                className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer"
              />
              <div className="flex justify-between text-xs text-gray-500 mt-1">
                <span>0.1s</span>
                <span>0.5s</span>
                <span>1.0s</span>
              </div>
              <p className="text-xs text-gray-600 mt-1">
                Silence detection for interim results
              </p>
            </div>
          )}

          {/* Preset Buttons */}
          {onUtteranceEndMsChange && onEndpointingMsChange && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Quick Presets
              </label>
              <div className="flex space-x-2">
                <button
                  onClick={() => {
                    onUtteranceEndMsChange(500)
                    onEndpointingMsChange(150)
                  }}
                  className="px-3 py-1 text-xs bg-blue-100 text-blue-800 rounded hover:bg-blue-200 transition-colors"
                >
                  Fast
                </button>
                <button
                  onClick={() => {
                    onUtteranceEndMsChange(1000)
                    onEndpointingMsChange(300)
                  }}
                  className="px-3 py-1 text-xs bg-green-100 text-green-800 rounded hover:bg-green-200 transition-colors"
                >
                  Normal
                </button>
                <button
                  onClick={() => {
                    onUtteranceEndMsChange(2000)
                    onEndpointingMsChange(500)
                  }}
                  className="px-3 py-1 text-xs bg-yellow-100 text-yellow-800 rounded hover:bg-yellow-200 transition-colors"
                >
                  Patient
                </button>
              </div>
              <p className="text-xs text-gray-600 mt-1">
                Fast: Quick responses, may cut off speech | Patient: Waits longer, better for slow speakers
              </p>
            </div>
          )}
        </div>
      )}

      {/* Audio Settings */}
      {showAudioSettings && (
        <div className="space-y-3">
          <h5 className="text-sm font-medium text-gray-700">Audio Settings</h5>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Sample Rate
            </label>
            <select
              value={sampleRate}
              onChange={(e) => onSampleRateChange?.(parseInt(e.target.value))}
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
            >
              <option value={8000}>8 kHz</option>
              <option value={16000}>16 kHz (Recommended)</option>
              <option value={44100}>44.1 kHz</option>
              <option value={48000}>48 kHz</option>
            </select>
            <p className="text-xs text-gray-600 mt-1">
              Higher sample rates provide better quality but use more bandwidth
            </p>
          </div>
        </div>
      )}

      {/* Info Box */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
        <div className="flex items-start space-x-2">
          <Info className="h-4 w-4 text-blue-600 mt-0.5" />
          <div className="text-sm text-blue-800">
            <p className="font-medium mb-1">Settings Tips</p>
            <ul className="text-xs space-y-1">
              <li>• Lower confidence thresholds include more uncertain matches</li>
              <li>• Higher thresholds are more selective but may miss some speakers</li>
              {showApiKeySection && <li>• Server-configured API keys are more secure</li>}
            </ul>
          </div>
        </div>
      </div>
    </div>
  )

  if (compact) {
    return (
      <div className={`bg-gray-50 border rounded-lg p-3 ${className}`}>
        <div className="space-y-3">
          {/* Compact confidence slider */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Confidence: {confidenceThreshold.toFixed(2)}
            </label>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={confidenceThreshold}
              onChange={(e) => onConfidenceThresholdChange(parseFloat(e.target.value))}
              className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer"
            />
          </div>

          {/* Compact toggles */}
          {showProcessingOptions && (
            <div className="flex items-center justify-between text-sm">
              {onEnableSpeakerIdentificationChange && (
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={enableSpeakerIdentification}
                    onChange={(e) => onEnableSpeakerIdentificationChange(e.target.checked)}
                    className="rounded border-gray-300 text-blue-600 mr-2"
                  />
                  <span>Speaker ID</span>
                </label>
              )}

              {onEnableTranscriptionChange && (
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={enableTranscription}
                    onChange={(e) => onEnableTranscriptionChange(e.target.checked)}
                    className="rounded border-gray-300 text-blue-600 mr-2"
                  />
                  <span>Transcription</span>
                </label>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className={`bg-gray-50 border rounded-lg p-4 ${className}`}>
      {renderHeader()}
      {(!collapsible || isExpanded) && (
        <div className="mt-4">
          {renderContent()}
        </div>
      )}
    </div>
  )
}

export default SettingsPanel
