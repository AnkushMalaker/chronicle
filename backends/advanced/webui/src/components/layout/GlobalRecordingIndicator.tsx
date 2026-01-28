import { useNavigate, useLocation } from 'react-router-dom'
import { Radio, Square, Zap, Archive } from 'lucide-react'
import { useRecording } from '../../contexts/RecordingContext'

export default function GlobalRecordingIndicator() {
  const navigate = useNavigate()
  const location = useLocation()
  const { isRecording, recordingDuration, mode, stopRecording, formatDuration } = useRecording()

  // Don't show if not recording
  if (!isRecording) return null

  // Don't show on the Live Record page (it has its own UI)
  if (location.pathname === '/live-record') return null

  return (
    <div className="flex items-center gap-3 px-3 py-1.5 bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg">
      {/* Pulsing red dot */}
      <div className="relative flex items-center">
        <span className="absolute inline-flex h-3 w-3 rounded-full bg-red-400 opacity-75 animate-ping" />
        <span className="relative inline-flex h-3 w-3 rounded-full bg-red-500" />
      </div>

      {/* Recording info */}
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium text-red-700 dark:text-red-300">
          {formatDuration(recordingDuration)}
        </span>
        <span className="text-red-600 dark:text-red-400 flex items-center gap-1">
          {mode === 'streaming' ? (
            <>
              <Zap className="h-3 w-3" />
              <span>Streaming</span>
            </>
          ) : (
            <>
              <Archive className="h-3 w-3" />
              <span>Batch</span>
            </>
          )}
        </span>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-1.5 ml-1">
        {/* Navigate to Live Record */}
        <button
          onClick={() => navigate('/live-record')}
          className="p-1.5 rounded hover:bg-red-100 dark:hover:bg-red-800/50 transition-colors text-red-600 dark:text-red-400"
          title="Go to Live Record"
        >
          <Radio className="h-4 w-4" />
        </button>

        {/* Stop button */}
        <button
          onClick={stopRecording}
          className="p-1.5 rounded bg-red-600 hover:bg-red-700 transition-colors text-white"
          title="Stop Recording"
        >
          <Square className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
