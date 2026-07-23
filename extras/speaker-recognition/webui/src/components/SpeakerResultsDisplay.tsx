/**
 * SpeakerResultsDisplay Component - Common result visualization
 * Displays speaker identification results across different processing modes
 * Supports diarization, transcription, and live streaming results
 */

import React from 'react'
import { Users, Clock, Download, Play, AlertTriangle } from 'lucide-react'
import { ProcessingResult, SpeakerSegment } from '../services/speakerIdentification'
import { TranscriptSegment } from '../hooks/useDeepgramIntegration'

export interface SpeakerResultsDisplayProps {
  // Processing results
  result?: ProcessingResult

  // Live streaming segments (alternative to result)
  liveSegments?: TranscriptSegment[]

  // Display options
  showTranscription?: boolean
  showAudioPlayback?: boolean
  showExport?: boolean
  showStats?: boolean
  compact?: boolean
  maxHeight?: string

  // Interaction handlers
  onExport?: (result: ProcessingResult) => void
  onPlaySegment?: (segment: SpeakerSegment | TranscriptSegment) => void

  // Styling
  className?: string
}

export const SpeakerResultsDisplay: React.FC<SpeakerResultsDisplayProps> = ({
  result,
  liveSegments = [],
  showTranscription = true,
  showAudioPlayback = false,
  showExport = true,
  showStats = true,
  compact = false,
  maxHeight = '400px',
  onExport,
  onPlaySegment,
  className = ''
}) => {
  // Determine data source
  const segments = result?.speakers || []
  const displaySegments = segments.length > 0 ? segments : liveSegments
  const isLiveMode = segments.length === 0 && liveSegments.length > 0

  // Helper functions
  const formatDuration = (seconds: number): string => {
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }

  const formatTime = (time: number): string => {
    const mins = Math.floor(time / 60)
    const secs = Math.floor(time % 60)
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }

  const getConfidenceColor = (confidence: number): string => {
    if (confidence >= 0.8) return 'text-green-600 bg-green-100'
    if (confidence >= 0.6) return 'text-yellow-600 bg-yellow-100'
    if (confidence >= 0.4) return 'text-orange-600 bg-orange-100'
    return 'text-red-600 bg-red-100'
  }

  const getConfidenceText = (confidence: number): string => {
    if (confidence >= 0.8) return 'High'
    if (confidence >= 0.6) return 'Medium'
    if (confidence >= 0.4) return 'Low'
    return 'Very Low'
  }

  // Render segment (works for both ProcessingResult segments and live segments)
  const renderSegment = (segment: SpeakerSegment | TranscriptSegment, index: number) => {
    const isProcessingSegment = 'speaker_id' in segment
    const isLiveSegment = 'isInterim' in segment

    // Extract common fields
    const start = (segment as SpeakerSegment).start || 0
    const end = isProcessingSegment
      ? (segment as SpeakerSegment).end
      : start // For live segments, we don't have a proper end time yet
    const duration = isProcessingSegment ? (end - start) : 0 // Only calculate duration for processed segments
    const text = isProcessingSegment ? (segment as SpeakerSegment).text : (segment as TranscriptSegment).text
    const confidence = segment.confidence
    const speakerName = isProcessingSegment
      ? (segment as SpeakerSegment).speaker_name
      : (segment as TranscriptSegment).speakerParts?.[0]?.speaker || 'N/A'

    const isInterim = isLiveSegment ? (segment as TranscriptSegment).isInterim : false

    // Create unique key to avoid React key conflicts when multiple segments have same speaker_id
    const uniqueKey = isProcessingSegment
      ? `${(segment as SpeakerSegment).speaker_id}_${index}_${start.toFixed(3)}`
      : `${(segment as TranscriptSegment).id}_${index}`

    return (
      <div
        key={uniqueKey}
        className={`border rounded-lg p-4 ${isInterim ? 'bg-gray-50 border-gray-300' : 'bg-white border-gray-200'} ${compact ? 'p-3' : 'p-4'}`}
      >
        {/* Header */}
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center space-x-2">
            {/* Speaker Info */}
            <div className="flex items-center space-x-2">
              <Users className="h-4 w-4 text-gray-500" />
              <span className={`font-medium ${speakerName === 'N/A' || speakerName === 'Unknown' ? 'text-gray-500' : 'text-blue-700'}`}>
                {speakerName}
              </span>
            </div>

            {/* Confidence Badge */}
            {confidence > 0 && (
              <span className={`px-2 py-1 rounded-full text-xs font-medium ${getConfidenceColor(confidence)}`}>
                {getConfidenceText(confidence)} ({(confidence * 100).toFixed(0)}%)
              </span>
            )}

            {/* Interim Badge */}
            {isInterim && (
              <span className="px-2 py-1 rounded-full text-xs font-medium text-blue-600 bg-blue-100">
                Live
              </span>
            )}
          </div>

          {/* Timing Info */}
          <div className="flex items-center space-x-2 text-sm text-gray-500">
            <Clock className="h-3 w-3" />
            {isLiveSegment ? (
              <span>Live</span>
            ) : (
              <>
                <span>{formatTime(start)}</span>
                {duration > 0 && (
                  <>
                    <span>-</span>
                    <span>{formatTime(end)}</span>
                    <span className="text-xs">({formatDuration(duration)})</span>
                  </>
                )}
              </>
            )}
          </div>
        </div>

        {/* Transcription */}
        {showTranscription && text && (
          <div className={`${isInterim ? 'text-gray-600 italic' : 'text-gray-900'}`}>
            <p className="leading-relaxed">"{text}"</p>
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center justify-between mt-3">
          <div className="flex items-center space-x-2">
            {/* Play Button */}
            {showAudioPlayback && onPlaySegment && !isInterim && (
              <button
                onClick={() => onPlaySegment(segment)}
                className="flex items-center space-x-1 px-2 py-1 text-sm text-blue-600 hover:text-blue-800 border border-blue-200 rounded hover:bg-blue-50"
                title="Play segment"
              >
                <Play className="h-3 w-3" />
                <span>Play</span>
              </button>
            )}
          </div>

          {/* Additional segment info for processing results */}
          {isProcessingSegment && (segment as SpeakerSegment).identified_speaker_name && (
            <div className="text-xs text-gray-500">
              Identified as: {(segment as SpeakerSegment).identified_speaker_name}
            </div>
          )}
        </div>
      </div>
    )
  }

  // Render stats summary
  const renderStats = () => {
    if (!showStats) return null

    let totalSegments = 0
    let highConfidence = 0
    let mediumConfidence = 0
    let lowConfidence = 0
    let uniqueSpeakers = new Set<string>()
    let totalDuration = 0

    if (result) {
      totalSegments = result.confidence_summary.total_segments
      highConfidence = result.confidence_summary.high_confidence
      mediumConfidence = result.confidence_summary.medium_confidence
      lowConfidence = result.confidence_summary.low_confidence

      result.speakers.forEach(seg => {
        uniqueSpeakers.add(seg.speaker_name)
        totalDuration = Math.max(totalDuration, seg.end)
      })
    } else {
      totalSegments = liveSegments.length
      liveSegments.forEach(seg => {
        if (seg.confidence >= 0.8) highConfidence++
        else if (seg.confidence >= 0.6) mediumConfidence++
        else if (seg.confidence >= 0.4) lowConfidence++

        if (seg.speakerParts) {
          seg.speakerParts.forEach(part => {
            if (part.speaker !== 'N/A') uniqueSpeakers.add(part.speaker)
          })
        }
      })
    }

    return (
      <div className={`bg-gray-50 border rounded-lg p-4 ${compact ? 'p-3' : 'p-4'}`}>
        <h4 className="font-medium text-gray-900 mb-3">📊 Analysis Summary</h4>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div className="text-center">
            <div className="text-2xl font-bold text-blue-600">{totalSegments}</div>
            <div className="text-gray-600">Segments</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-green-600">{uniqueSpeakers.size}</div>
            <div className="text-gray-600">Speakers</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-purple-600">{highConfidence}</div>
            <div className="text-gray-600">High Conf.</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-orange-600">
              {totalDuration > 0 ? formatDuration(totalDuration) : 'Live'}
            </div>
            <div className="text-gray-600">Duration</div>
          </div>
        </div>

        {result && (
          <div className="mt-3 text-xs text-gray-500">
            Processing mode: <span className="font-medium">{result.mode}</span>
            {result.processing_time && (
              <span> • Processing time: {(result.processing_time / 1000).toFixed(1)}s</span>
            )}
          </div>
        )}
      </div>
    )
  }

  if (displaySegments.length === 0) {
    return (
      <div className={`text-center py-8 ${className}`}>
        <Users className="h-12 w-12 text-primary mx-auto mb-2" />
        <h3 className="heading-sm mb-2">
          {isLiveMode ? 'No Live Transcription Yet' : 'No Results'}
        </h3>
        <p className="text-muted">
          {isLiveMode
            ? 'Start speaking to see live transcription and speaker identification'
            : 'Process audio to see speaker identification results'}
        </p>
      </div>
    )
  }

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-medium text-gray-900">
          {isLiveMode ? '🎙️ Live Results' : '🎯 Speaker Analysis Results'}
        </h3>

        {/* Export Button */}
        {showExport && result && onExport && (
          <button
            onClick={() => onExport(result)}
            className="flex items-center space-x-2 px-3 py-2 text-sm text-green-600 border border-green-200 rounded hover:bg-green-50"
          >
            <Download className="h-4 w-4" />
            <span>Export</span>
          </button>
        )}
      </div>

      {/* Stats Summary */}
      {renderStats()}

      {/* Results Status */}
      {result?.status === 'failed' && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <div className="flex items-center space-x-2">
            <AlertTriangle className="h-5 w-5 text-red-600" />
            <div>
              <h4 className="text-red-800 font-medium">Processing Failed</h4>
              <p className="text-red-600 text-sm">{result.error}</p>
            </div>
          </div>
        </div>
      )}

      {/* Segments List */}
      <div className="space-y-3" style={{ maxHeight, overflowY: 'auto' }}>
        {compact ? (
          /* Compact view */
          <div className="space-y-2">
            {displaySegments.map((segment, index) => renderSegment(segment, index))}
          </div>
        ) : (
          /* Full view */
          <div className="space-y-4">
            {displaySegments.map((segment, index) => renderSegment(segment, index))}
          </div>
        )}
      </div>

      {/* Auto-scroll indicator for live mode */}
      {isLiveMode && liveSegments.length > 0 && (
        <div className="text-center">
          <div className="inline-flex items-center space-x-2 px-3 py-1 bg-blue-100 text-blue-800 rounded-full text-sm">
            <div className="w-2 h-2 bg-blue-600 rounded-full animate-pulse"></div>
            <span>Live transcription active</span>
          </div>
        </div>
      )}
    </div>
  )
}

export default SpeakerResultsDisplay
