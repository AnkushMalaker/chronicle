/**
 * ProcessingModeSelector Component - Unified processing mode selection
 * Provides consistent interface for selecting processing modes across all pages
 * Supports diarization, deepgram, hybrid, and plain modes
 */

import React from 'react'
import { Settings, Info } from 'lucide-react'
import { ProcessingMode } from '../services/speakerIdentification'
import { ProcessedAudio } from '../services/audioProcessing'

export interface ProcessingModeConfig {
  mode: ProcessingMode
  name: string
  description: string
  icon?: string
  color: string
  requirements?: string[]
  features: string[]
}

export interface ProcessingModeSelectorProps {
  selectedMode: ProcessingMode
  onModeChange: (mode: ProcessingMode) => void
  onProcessAudio: (mode: ProcessingMode) => Promise<void>
  audioData: ProcessedAudio | null
  isProcessing: boolean
  confidenceThreshold: number
  onConfidenceThresholdChange: (threshold: number) => void
  minSpeakers: number
  onMinSpeakersChange: (min: number) => void
  maxSpeakers: number
  onMaxSpeakersChange: (max: number) => void
  collar?: number
  onCollarChange?: (collar: number) => void
  minDurationOff?: number
  onMinDurationOffChange?: (minDurationOff: number) => void
  uploadedJson?: any
  showSettings?: boolean
  compact?: boolean
  className?: string
}

const PROCESSING_MODES: ProcessingModeConfig[] = [
  {
    mode: 'speaker-identification',
    name: 'Speaker Identification',
    description: 'Diarization + speaker recognition only',
    icon: '🎯',
    color: 'bg-blue-600 hover:bg-blue-700',
    features: ['Speaker diarization', 'Speaker identification', 'Confidence scoring']
  },
  {
    mode: 'deepgram-enhanced',
    name: 'Transcribe + Identify',
    description: 'Full transcription with enhanced speaker ID',
    icon: '🚀',
    color: 'bg-green-600 hover:bg-green-700',
    requirements: ['Deepgram API key'],
    features: ['High-quality transcription', 'Speaker diarization', 'Enhanced speaker identification', 'Word-level timing']
  },
  {
    mode: 'deepgram-transcript-internal-speakers',
    name: 'Hybrid Mode',
    description: 'Deepgram transcription + internal diarization',
    icon: '🔄',
    color: 'bg-purple-600 hover:bg-purple-700',
    requirements: ['Deepgram API key'],
    features: ['Best transcription quality', 'Accurate speaker segmentation', 'Enhanced identification', 'Optimal accuracy']
  },
  {
    mode: 'diarize-identify-match',
    name: 'Transcript + Diarize',
    description: 'Match transcript to internal diarization + speaker ID',
    icon: '🔗',
    color: 'bg-orange-600 hover:bg-orange-700',
    features: ['Uses existing transcript', 'Internal speaker diarization', 'Speaker identification', 'Word-to-segment matching']
  },
]

export const ProcessingModeSelector: React.FC<ProcessingModeSelectorProps> = ({
  selectedMode,
  onModeChange,
  onProcessAudio,
  audioData,
  isProcessing,
  confidenceThreshold,
  onConfidenceThresholdChange,
  minSpeakers,
  onMinSpeakersChange,
  maxSpeakers,
  onMaxSpeakersChange,
  collar = 2.0,
  onCollarChange,
  minDurationOff = 1.5,
  onMinDurationOffChange,
  uploadedJson,
  showSettings = true,
  compact = false,
  className = ''
}) => {
  const selectedConfig = PROCESSING_MODES.find(m => m.mode === selectedMode) || PROCESSING_MODES[0]

  const handleProcessAudio = async (mode: ProcessingMode) => {
    if (!audioData) return
    await onProcessAudio(mode)
  }

  if (compact) {
    return (
      <div className={`space-y-3 ${className}`}>
        {/* Mode Selection Dropdown */}
        <div>
          <label className="input-label mb-1">
            Processing Mode
          </label>
          <select
            value={selectedMode}
            onChange={(e) => {
              const newMode = e.target.value as ProcessingMode
              // Prevent selecting diarize-identify-match without transcript
              if (newMode === 'diarize-identify-match' && !uploadedJson) {
                return // Don't change the mode
              }
              onModeChange(newMode)
            }}
            className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
            disabled={isProcessing}
          >
            {PROCESSING_MODES.map((config) => {
              const isTranscriptMode = config.mode === 'diarize-identify-match'
              const isDisabled = isTranscriptMode && !uploadedJson
              const displayName = isTranscriptMode && !uploadedJson
                ? `${config.icon} ${config.name} (upload transcript)`
                : `${config.icon} ${config.name}`

              return (
                <option
                  key={config.mode}
                  value={config.mode}
                  disabled={isDisabled}
                  style={isDisabled ? { color: '#9CA3AF' } : {}}
                >
                  {displayName}
                </option>
              )
            })}
          </select>
        </div>

        {/* Confidence Threshold */}
        {showSettings && (
          <>
            <div>
              <label className="input-label mb-1">
                Confidence Threshold: {confidenceThreshold.toFixed(2)}
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={confidenceThreshold}
                onChange={(e) => onConfidenceThresholdChange(parseFloat(e.target.value))}
                className="w-full"
                disabled={isProcessing}
              />
              <div className="flex justify-between text-xs text-muted mt-1">
                <span>Less Strict</span>
                <span>More Strict</span>
              </div>
            </div>

            {/* Min Speakers */}
            <div>
              <label className="input-label mb-1">
                Min Speakers: {minSpeakers}
              </label>
              <input
                type="range"
                min="1"
                max="8"
                step="1"
                value={minSpeakers}
                onChange={(e) => {
                  const newMin = parseInt(e.target.value)
                  onMinSpeakersChange(newMin)
                  // Ensure max is at least min
                  if (maxSpeakers < newMin) {
                    onMaxSpeakersChange(newMin)
                  }
                }}
                className="w-full"
                disabled={isProcessing}
              />
              <div className="flex justify-between text-xs text-muted mt-1">
                <span>1</span>
                <span>8</span>
              </div>
            </div>

            {/* Max Speakers */}
            <div>
              <label className="input-label mb-1">
                Max Speakers: {maxSpeakers}
              </label>
              <input
                type="range"
                min="1"
                max="8"
                step="1"
                value={maxSpeakers}
                onChange={(e) => {
                  const newMax = parseInt(e.target.value)
                  onMaxSpeakersChange(newMax)
                  // Ensure min is at most max
                  if (minSpeakers > newMax) {
                    onMinSpeakersChange(newMax)
                  }
                }}
                className="w-full"
                disabled={isProcessing}
              />
              <div className="flex justify-between text-xs text-muted mt-1">
                <span>1</span>
                <span>8</span>
              </div>
            </div>

            {/* Collar Parameter */}
            {onCollarChange && (
              <div>
                <label className="input-label mb-1">
                  Segment Collar: {collar.toFixed(1)}s
                </label>
                <input
                  type="range"
                  min="0"
                  max="5"
                  step="0.5"
                  value={collar}
                  onChange={(e) => onCollarChange(parseFloat(e.target.value))}
                  className="w-full"
                  disabled={isProcessing}
                />
                <div className="flex justify-between text-xs text-muted mt-1">
                  <span>0s</span>
                  <span>5s</span>
                </div>
                <p className="text-xs text-muted mt-1">
                  Merges segments separated by gaps shorter than this value
                </p>
              </div>
            )}

            {/* Min Duration Off Parameter */}
            {onMinDurationOffChange && (
              <div>
                <label className="input-label mb-1">
                  Min Silence Duration: {minDurationOff.toFixed(1)}s
                </label>
                <input
                  type="range"
                  min="0"
                  max="3"
                  step="0.1"
                  value={minDurationOff}
                  onChange={(e) => onMinDurationOffChange(parseFloat(e.target.value))}
                  className="w-full"
                  disabled={isProcessing}
                />
                <div className="flex justify-between text-xs text-muted mt-1">
                  <span>0s</span>
                  <span>3s</span>
                </div>
                <p className="text-xs text-muted mt-1">
                  Minimum silence before treating as segment boundary
                </p>
              </div>
            )}
          </>
        )}

        {/* Process Button */}
        <button
          onClick={() => handleProcessAudio(selectedMode)}
          disabled={!audioData || isProcessing}
          className={`w-full flex items-center justify-center space-x-2 px-4 py-2 text-white rounded-md disabled:opacity-50 disabled:cursor-not-allowed ${selectedConfig.color}`}
        >
          <span>{selectedConfig.icon}</span>
          <span>{isProcessing ? 'Processing...' : `Start ${selectedConfig.name}`}</span>
        </button>
      </div>
    )
  }

  return (
    <div className={`space-y-6 ${className}`}>
      {/* Settings Panel */}
      {showSettings && (
        <div className="card p-4">
          <div className="flex items-center space-x-2 mb-3">
            <Settings className="h-4 w-4 text-gray-600 dark:text-gray-400" />
            <h4 className="font-medium text-primary">Processing Settings</h4>
          </div>

          <div className="space-y-4">
            <div>
              <label className="input-label dark:text-gray-300 mb-1">
                Confidence Threshold: {confidenceThreshold.toFixed(2)}
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={confidenceThreshold}
                onChange={(e) => onConfidenceThresholdChange(parseFloat(e.target.value))}
                className="w-full"
                disabled={isProcessing}
              />
              <div className="flex justify-between text-xs text-muted mt-1">
                <span>Less Strict</span>
                <span>More Strict</span>
              </div>
            </div>

            {/* Min Speakers */}
            <div>
              <label className="input-label mb-1">
                Min Speakers: {minSpeakers}
              </label>
              <input
                type="range"
                min="1"
                max="8"
                step="1"
                value={minSpeakers}
                onChange={(e) => {
                  const newMin = parseInt(e.target.value)
                  onMinSpeakersChange(newMin)
                  // Ensure max is at least min
                  if (maxSpeakers < newMin) {
                    onMaxSpeakersChange(newMin)
                  }
                }}
                className="w-full"
                disabled={isProcessing}
              />
              <div className="flex justify-between text-xs text-muted mt-1">
                <span>1</span>
                <span>8</span>
              </div>
            </div>

            {/* Max Speakers */}
            <div>
              <label className="input-label mb-1">
                Max Speakers: {maxSpeakers}
              </label>
              <input
                type="range"
                min="1"
                max="8"
                step="1"
                value={maxSpeakers}
                onChange={(e) => {
                  const newMax = parseInt(e.target.value)
                  onMaxSpeakersChange(newMax)
                  // Ensure min is at most max
                  if (minSpeakers > newMax) {
                    onMinSpeakersChange(newMax)
                  }
                }}
                className="w-full"
                disabled={isProcessing}
              />
              <div className="flex justify-between text-xs text-muted mt-1">
                <span>1</span>
                <span>8</span>
              </div>
            </div>

            {/* Collar Parameter */}
            {onCollarChange && (
              <div>
                <label className="input-label mb-1">
                  Segment Collar: {collar.toFixed(1)}s
                </label>
                <input
                  type="range"
                  min="0"
                  max="5"
                  step="0.5"
                  value={collar}
                  onChange={(e) => onCollarChange(parseFloat(e.target.value))}
                  className="w-full"
                  disabled={isProcessing}
                />
                <div className="flex justify-between text-xs text-muted mt-1">
                  <span>0s</span>
                  <span>5s</span>
                </div>
                <p className="text-xs text-muted mt-1">
                  Merges segments separated by gaps shorter than this value
                </p>
              </div>
            )}

            {/* Min Duration Off Parameter */}
            {onMinDurationOffChange && (
              <div>
                <label className="input-label mb-1">
                  Min Silence Duration: {minDurationOff.toFixed(1)}s
                </label>
                <input
                  type="range"
                  min="0"
                  max="3"
                  step="0.1"
                  value={minDurationOff}
                  onChange={(e) => onMinDurationOffChange(parseFloat(e.target.value))}
                  className="w-full"
                  disabled={isProcessing}
                />
                <div className="flex justify-between text-xs text-muted mt-1">
                  <span>0s</span>
                  <span>3s</span>
                </div>
                <p className="text-xs text-muted mt-1">
                  Minimum silence before treating as segment boundary
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Mode Selection */}
      <div className="space-y-4">
        <h4 className="font-medium text-gray-900">Choose Processing Mode</h4>

        {/* Grid Layout for Modes */}
        <div className="grid md:grid-cols-2 gap-4">
          {PROCESSING_MODES.slice(0, 2).map((config) => (
            <div
              key={config.mode}
              className={`border rounded-lg p-4 cursor-pointer transition-colors ${
                selectedMode === config.mode
                  ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 dark:border-blue-400'
                  : 'border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
              }`}
              onClick={() => onModeChange(config.mode)}
            >
              <div className="flex items-start space-x-3">
                <span className="text-2xl">{config.icon}</span>
                <div className="flex-1">
                  <h5 className="heading-sm">{config.name}</h5>
                  <p className="text-sm text-secondary mt-1">{config.description}</p>

                  {/* Requirements */}
                  {config.requirements && (
                    <div className="mt-2">
                      <span className="inline-flex items-center px-2 py-1 rounded-full text-xs bg-yellow-100 text-yellow-800">
                        Requires: {config.requirements.join(', ')}
                      </span>
                    </div>
                  )}

                  {/* Features */}
                  <div className="mt-2">
                    <div className="flex flex-wrap gap-1">
                      {config.features.slice(0, 2).map((feature, index) => (
                        <span key={index} className="text-xs text-muted">
                          • {feature}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>

        {/* Full-width mode for hybrid */}
        <div
          className={`border rounded-lg p-4 cursor-pointer transition-colors ${
            selectedMode === 'deepgram-transcript-internal-speakers'
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 dark:border-blue-400'
              : 'border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
          }`}
          onClick={() => onModeChange('deepgram-transcript-internal-speakers')}
        >
          <div className="flex items-start space-x-3">
            <span className="text-2xl">🔄</span>
            <div className="flex-1">
              <h5 className="heading-sm">Hybrid: Deepgram Transcription + Internal Diarization</h5>
              <p className="text-sm text-secondary mt-1">Best of both: High-quality transcription with accurate speaker segmentation</p>

              <div className="mt-2">
                <span className="inline-flex items-center px-2 py-1 rounded-full text-xs bg-yellow-100 text-yellow-800">
                  Requires: Deepgram API key
                </span>
              </div>

              <div className="mt-2">
                <div className="flex flex-wrap gap-1">
                  {PROCESSING_MODES[2].features.map((feature, index) => (
                    <span key={index} className="text-xs text-muted">
                      • {feature}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>

      </div>

      {/* Selected Mode Info */}
      {selectedConfig && (
        <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-700 rounded-lg p-4">
          <div className="flex items-center space-x-2 mb-2">
            <Info className="h-4 w-4 text-blue-600 dark:text-blue-400" />
            <h5 className="font-medium text-blue-900 dark:text-blue-100">Selected: {selectedConfig.name}</h5>
          </div>
          <p className="text-sm text-blue-800 dark:text-blue-200 mb-3">{selectedConfig.description}</p>

          <div className="space-y-2">
            <div>
              <span className="text-sm font-medium text-blue-900 dark:text-blue-100">Features:</span>
              <ul className="text-sm text-blue-800 dark:text-blue-200 ml-4 mt-1">
                {selectedConfig.features.map((feature, index) => (
                  <li key={index}>• {feature}</li>
                ))}
              </ul>
            </div>

            {selectedConfig.requirements && (
              <div>
                <span className="text-sm font-medium text-blue-900 dark:text-blue-100">Requirements:</span>
                <ul className="text-sm text-blue-800 dark:text-blue-200 ml-4 mt-1">
                  {selectedConfig.requirements.map((req, index) => (
                    <li key={index}>• {req}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Process Button */}
      <button
        onClick={() => handleProcessAudio(selectedMode)}
        disabled={!audioData || isProcessing}
        className={`w-full flex items-center justify-center space-x-2 px-6 py-4 text-white rounded-md disabled:opacity-50 disabled:cursor-not-allowed text-lg font-medium ${selectedConfig.color}`}
      >
        <span className="text-xl">{selectedConfig.icon}</span>
        <span>{isProcessing ? 'Processing...' : `Start ${selectedConfig.name}`}</span>
      </button>
    </div>
  )
}

export default ProcessingModeSelector
