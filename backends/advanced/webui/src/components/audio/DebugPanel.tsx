import { UseAudioRecordingReturn } from '../../hooks/useAudioRecording'

interface DebugPanelProps {
  recording: UseAudioRecordingReturn
}

export default function DebugPanel({ recording }: DebugPanelProps) {
  return (
    <div className="mt-6 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <h3 className="font-medium text-gray-800 dark:text-gray-200 mb-3 flex items-center">
        🐛 Debug Information
      </h3>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
          <p className="font-medium text-gray-700 dark:text-gray-300">WebSocket</p>
          <p className="text-gray-600 dark:text-gray-400">
            State: {recording.hasValidWebSocket ? 'Connected' : 'Disconnected'}
          </p>
          <p className="text-gray-600 dark:text-gray-400">
            Attempts: {recording.debugStats.connectionAttempts}
          </p>
        </div>
        
        <div>
          <p className="font-medium text-gray-700 dark:text-gray-300">Audio Chunks</p>
          <p className="text-gray-600 dark:text-gray-400">
            Sent: {recording.debugStats.chunksSent}
          </p>
          <p className="text-gray-600 dark:text-gray-400">
            Rate: {recording.debugStats.chunksSent > 0 && recording.debugStats.sessionStartTime ? 
              Math.round(recording.debugStats.chunksSent / ((Date.now() - recording.debugStats.sessionStartTime.getTime()) / 1000)) : 0}/s
          </p>
        </div>

        <div>
          <p className="font-medium text-gray-700 dark:text-gray-300">Messages</p>
          <p className="text-gray-600 dark:text-gray-400">
            Received: {recording.debugStats.messagesReceived}
          </p>
          <p className="text-gray-600 dark:text-gray-400">
            Audio Context: {recording.hasValidAudioContext ? 'Active' : 'Inactive'}
          </p>
        </div>

        <div>
          <p className="font-medium text-gray-700 dark:text-gray-300">Session</p>
          <p className="text-gray-600 dark:text-gray-400">
            Duration: {recording.debugStats.sessionStartTime ? 
              Math.round((Date.now() - recording.debugStats.sessionStartTime.getTime()) / 1000) + 's' : 'N/A'}
          </p>
          <p className="text-gray-600 dark:text-gray-400">
            Processing: {recording.audioProcessingStarted ? 'Yes' : 'No'}
          </p>
        </div>
      </div>

      {recording.debugStats.lastError && (
        <div className="mt-3 p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded">
          <p className="text-sm font-medium text-red-700 dark:text-red-300">Last Error:</p>
          <p className="text-sm text-red-600 dark:text-red-400">{recording.debugStats.lastError}</p>
          <p className="text-xs text-red-500 dark:text-red-500">
            {recording.debugStats.lastErrorTime?.toLocaleTimeString()}
          </p>
        </div>
      )}

      <div className="mt-3 text-xs text-gray-500 dark:text-gray-500">
        <p>• WebSocket URL: {recording.hasValidWebSocket ? '/ws?codec=pcm endpoint' : 'Not connected'}</p>
        <p>• Audio Format: 16kHz, Mono, PCM Int16</p>
        <p>• Protocol: Wyoming (JSON headers + binary payloads)</p>
        <p>• Direct Checks: WS={recording.hasValidWebSocket ? '✅' : '❌'} Mic={recording.hasValidMicrophone ? '✅' : '❌'} Ctx={recording.hasValidAudioContext ? '✅' : '❌'}</p>
      </div>
    </div>
  )
}