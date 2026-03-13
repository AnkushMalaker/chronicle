/**
 * Component for displaying live transcript with speaker identification
 */

import React, { useRef, useEffect } from 'react'
import { Mic } from 'lucide-react'

export interface SpeakerPart {
  speaker: string
  text: string
  confidence: number
}

export interface TranscriptSegment {
  id: string
  timestamp: number
  speaker: number
  text: string
  confidence: number
  isInterim: boolean
  speakerParts?: SpeakerPart[]
}

export interface LiveTranscriptProps {
  segments: TranscriptSegment[]
}

function TranscriptSegmentComponent({ segment }: { segment: TranscriptSegment }) {
  return (
    <div
      className={`p-3 rounded-lg transition-all ${
        segment.isInterim
          ? 'bg-gray-50 border-l-4 border-yellow-400 opacity-70'
          : 'bg-blue-50 border-l-4 border-blue-400'
      }`}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center space-x-2">
          {segment.isInterim && (
            <span className="text-xs text-yellow-600 bg-yellow-100 px-2 py-1 rounded-full">
              typing...
            </span>
          )}
        </div>
        <div className="text-xs text-gray-400">
          {new Date(segment.timestamp).toLocaleTimeString()}
        </div>
      </div>

      <div className={`${segment.isInterim ? 'text-gray-600 italic' : 'text-gray-900'}`}>
        {segment.speakerParts?.map((part, partIndex) => (
          <span key={partIndex} className="inline-block mr-2">
            <span className={`font-semibold ${
              part.speaker === 'N/A'
                ? 'text-gray-500'
                : 'text-blue-700'
            }`}>
              {part.speaker}:
            </span>
            <span className="ml-1">
              "{part.text}"
            </span>
            {part.speaker !== 'N/A' && part.confidence > 0 && (
              <span className="text-xs text-green-600 bg-green-100 px-1 py-0.5 rounded ml-1">
                {(part.confidence * 100).toFixed(0)}%
              </span>
            )}
          </span>
        )) || (
          <span className="text-gray-500">
            <span className="font-semibold">N/A:</span>
            <span className="ml-1">"{segment.text}"</span>
          </span>
        )}
      </div>
    </div>
  )
}

function EmptyTranscriptState() {
  return (
    <div className="text-center py-8 text-gray-500">
      <Mic className="h-12 w-12 mx-auto mb-2 opacity-50" />
      <p>Start a session to see live transcription</p>
    </div>
  )
}

export function LiveTranscript({ segments }: LiveTranscriptProps) {
  const transcriptEndRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom when new segments arrive
  useEffect(() => {
    if (transcriptEndRef.current) {
      transcriptEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [segments])

  return (
    <div className="bg-white border rounded-lg">
      <div className="p-4 border-b">
        <h3 className="text-lg font-medium text-gray-900">Live Transcript</h3>
      </div>

      <div className="p-4 max-h-96 overflow-y-auto">
        {segments.length === 0 ? (
          <EmptyTranscriptState />
        ) : (
          <div className="space-y-2">
            {segments.map((segment) => (
              <TranscriptSegmentComponent key={segment.id} segment={segment} />
            ))}
            <div ref={transcriptEndRef} />
          </div>
        )}
      </div>
    </div>
  )
}
