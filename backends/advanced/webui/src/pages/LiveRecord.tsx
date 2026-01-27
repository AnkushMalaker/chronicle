import { Radio, Zap, Archive, Database } from 'lucide-react'
import { useSimpleAudioRecording } from '../hooks/useSimpleAudioRecording'
import SimplifiedControls from '../components/audio/SimplifiedControls'
import StatusDisplay from '../components/audio/StatusDisplay'
import AudioVisualizer from '../components/audio/AudioVisualizer'
import SimpleDebugPanel from '../components/audio/SimpleDebugPanel'

export default function LiveRecord() {
  const recording = useSimpleAudioRecording()

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-2">
          <Radio className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Live Audio Recording
          </h1>
        </div>

        {/* Mode Toggle */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => recording.setMode('streaming')}
            disabled={recording.isRecording}
            className={`
              flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
              ${recording.mode === 'streaming'
                ? 'bg-blue-600 text-white shadow-md'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-300 dark:hover:bg-gray-600'
              }
              ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
            `}
          >
            <Zap className="h-4 w-4" />
            <span>Streaming</span>
          </button>
          <button
            onClick={() => recording.setMode('batch')}
            disabled={recording.isRecording}
            className={`
              flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
              ${recording.mode === 'batch'
                ? 'bg-blue-600 text-white shadow-md'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-300 dark:hover:bg-gray-600'
              }
              ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
            `}
          >
            <Archive className="h-4 w-4" />
            <span>Batch</span>
          </button>
        </div>
      </div>

      {/* Always Persist Audio Toggle */}
      <div className="mb-4 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <label className="flex items-center justify-between cursor-pointer">
          <div className="flex items-center space-x-3">
            <Database className="w-5 h-5 text-blue-500" />
            <div>
              <div className="font-medium text-gray-900 dark:text-white">
                Always Persist Audio
              </div>
              <div className="text-sm text-gray-500 dark:text-gray-400">
                Save audio to database even if transcription fails
              </div>
            </div>
          </div>
          <div className="relative">
            <input
              type="checkbox"
              checked={recording.alwaysPersist}
              onChange={(e) => recording.setAlwaysPersist(e.target.checked)}
              disabled={recording.isRecording}
              className="sr-only peer"
            />
            <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-700 peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
          </div>
        </label>
      </div>

      {/* Mode Description */}
      <div className="mb-4 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3">
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {recording.mode === 'streaming' ? (
            <>
              <strong>Streaming Mode:</strong> Audio is sent in real-time chunks and processed immediately.
              Transcription starts while you're still speaking.
            </>
          ) : (
            <>
              <strong>Batch Mode:</strong> Audio is accumulated and sent as a complete file when you stop recording.
              Transcription begins after recording ends.
            </>
          )}
        </p>
      </div>

      {/* Main Controls - Single START button */}
      <SimplifiedControls recording={recording} />

      {/* Status Display - Shows setup progress */}
      <StatusDisplay recording={recording} />

      {/* Audio Visualizer - Shows waveform when recording */}
      <AudioVisualizer 
        isRecording={recording.isRecording}
        analyser={recording.analyser}
      />

      {/* Instructions */}
      <div className="mt-8 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
        <h3 className="font-medium text-blue-800 dark:text-blue-200 mb-2">
          📝 How it Works
        </h3>
        <ul className="text-sm text-blue-700 dark:text-blue-300 space-y-1">
          <li>• <strong>Choose your mode:</strong> Streaming for real-time or Batch for complete file processing</li>
          <li>• <strong>One-click recording:</strong> Single button handles complete setup automatically</li>
          <li>• <strong>Sequential process:</strong> Mic access → WebSocket connection → Audio session → Recording</li>
          <li>• <strong>Mode-based processing:</strong>
            {recording.mode === 'streaming'
              ? 'Real-time chunks sent as you speak'
              : 'Complete audio sent after you stop'
            }
          </li>
          <li>• <strong>Wyoming protocol:</strong> Structured communication ensures reliable data transmission</li>
          <li>• <strong>High quality audio:</strong> 16kHz mono with noise suppression and echo cancellation</li>
          <li>• <strong>View results:</strong> Check Conversations page for transcribed content and memories</li>
        </ul>
      </div>

      {/* Debug Information Panel */}
      <SimpleDebugPanel recording={recording} />
    </div>
  )
}