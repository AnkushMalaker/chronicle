import { Radio, Zap, Archive, Settings, Monitor, Mic } from 'lucide-react'
import { useRecording } from '../contexts/RecordingContext'
import SimplifiedControls from '../components/audio/SimplifiedControls'
import StatusDisplay from '../components/audio/StatusDisplay'
import AudioVisualizer from '../components/audio/AudioVisualizer'
import SimpleDebugPanel from '../components/audio/SimpleDebugPanel'
import WakeFeedback from '../components/audio/WakeFeedback'

export default function LiveRecord() {
  const recording = useRecording()

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

      {/* Audio Source Toggle */}
      <div className="mb-4 flex items-center gap-3">
        <div className={`inline-flex rounded-lg border border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-800 p-0.5 ${recording.isRecording ? 'opacity-50 pointer-events-none' : ''}`}>
          <button
            onClick={() => recording.setAudioSource('mic')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'mic'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Mic className="h-3.5 w-3.5" />
            <span>Mic</span>
          </button>
          <button
            onClick={() => recording.setAudioSource('meeting')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'meeting'
                ? 'bg-purple-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Mic className="h-3.5 w-3.5" />
            <Monitor className="h-3.5 w-3.5" />
            <span>Meeting</span>
          </button>
          <button
            onClick={() => recording.setAudioSource('tab')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'tab'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Monitor className="h-3.5 w-3.5" />
            <span>Tab</span>
          </button>
        </div>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {recording.audioSource === 'mic'
            ? 'Microphone only'
            : recording.audioSource === 'meeting'
              ? 'Mic + tab audio (you\'ll be asked to select a tab)'
              : 'Browser tab audio only (no microphone)'}
        </span>
      </div>

      {/* Microphone Selector (hidden in tab-only mode) */}
      {recording.audioSource !== 'tab' && recording.availableDevices.length > 1 && (
        <div className="mb-4 flex items-center gap-2">
          <Settings className="h-4 w-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-shrink-0">
            Microphone:
          </label>
          <select
            value={recording.selectedDeviceId ?? ''}
            onChange={(e) => recording.setSelectedDeviceId(e.target.value || null)}
            disabled={recording.isRecording}
            className={`
              flex-1 min-w-0 text-sm px-2 py-1.5 rounded-lg border
              bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100
              border-gray-300 dark:border-gray-600
              ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
            `}
          >
            <option value="">System Default</option>
            {recording.availableDevices.map((device) => (
              <option key={device.deviceId} value={device.deviceId}>
                {device.label || `Microphone (${device.deviceId.slice(0, 8)}...)`}
              </option>
            ))}
          </select>
        </div>
      )}

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

      {/* Wake-word feedback - pulses on arm/end-of-turn + shows recognized command */}
      <WakeFeedback />

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
