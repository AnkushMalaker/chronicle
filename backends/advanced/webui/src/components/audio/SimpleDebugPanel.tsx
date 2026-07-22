import { RecordingContextType } from '../../contexts/RecordingContext'
import { Card } from '../ui'

interface SimpleDebugPanelProps {
  recording: RecordingContextType
}

export default function SimpleDebugPanel({ recording }: SimpleDebugPanelProps) {
  return (
    <Card className="mt-6 bg-gray-50 dark:bg-gray-900">
      <h3 className="font-medium text-gray-800 dark:text-gray-200 mb-3 flex items-center">
        🐛 Debug Information
      </h3>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
          <p className="font-medium text-gray-700 dark:text-gray-300">Current Step</p>
          <p className="text-gray-600 dark:text-gray-400">
            Step: {recording.currentStep}
          </p>
          <p className="text-gray-600 dark:text-gray-400">
            Recording: {recording.isRecording ? 'Yes' : 'No'}
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
            Attempts: {recording.debugStats.connectionAttempts}
          </p>
        </div>

        <div>
          <p className="font-medium text-gray-700 dark:text-gray-300">Session</p>
          <p className="text-gray-600 dark:text-gray-400">
            Duration: {recording.debugStats.sessionStartTime ?
              Math.round((Date.now() - recording.debugStats.sessionStartTime.getTime()) / 1000) + 's' : 'N/A'}
          </p>
          <p className="text-gray-600 dark:text-gray-400">
            Security: {recording.canAccessMicrophone ? 'OK' : 'Blocked'}
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
        <p>• Protocol: Wyoming (JSON headers + binary payloads)</p>
        <p>• Audio Format: 16kHz, Mono, PCM Int16</p>
        <p>• Sequential Flow: Mic → WebSocket → Audio-Start → Streaming</p>
        <p>• Security: {recording.canAccessMicrophone ? '✅ HTTPS/Localhost' : '❌ Insecure Connection'}</p>
      </div>
    </Card>
  )
}
