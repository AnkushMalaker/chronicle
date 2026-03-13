/**
 * Component for displaying live session statistics
 */

import React from 'react'
import { Clock, Volume2, Users } from 'lucide-react'
import { formatDuration } from '../../utils/common'

export interface LiveStats {
  totalWords: number
  averageConfidence: number
  identifiedSpeakers: Set<string>
  sessionDuration: number
}

export interface SessionStatsProps {
  stats: LiveStats
  isRecording: boolean
}

export function SessionStats({ stats, isRecording }: SessionStatsProps) {
  if (!isRecording) {
    return null
  }

  return (
    <div className="bg-white border rounded-lg p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-6">
          <div className="flex items-center space-x-2">
            <Clock className="h-4 w-4 text-gray-500" />
            <span className="text-sm text-gray-900">
              {formatDuration(stats.sessionDuration)}
            </span>
          </div>
          <div className="flex items-center space-x-2">
            <Volume2 className="h-4 w-4 text-gray-500" />
            <span className="text-sm text-gray-900">
              {stats.totalWords} words
            </span>
          </div>
          <div className="flex items-center space-x-2">
            <Users className="h-4 w-4 text-gray-500" />
            <span className="text-sm text-gray-900">
              {stats.identifiedSpeakers.size} speakers
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}
