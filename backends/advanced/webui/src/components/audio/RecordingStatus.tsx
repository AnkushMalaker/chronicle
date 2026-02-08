import { Wifi, WifiOff, Radio } from 'lucide-react'
import { UseAudioRecordingReturn } from '../../hooks/useAudioRecording'
import { useAuth } from '../../contexts/AuthContext'

interface RecordingStatusProps {
  recording: UseAudioRecordingReturn
}

export default function RecordingStatus({ recording }: RecordingStatusProps) {
  const { user } = useAuth()
  
  const getStatusIcon = () => {
    switch (recording.connectionStatus) {
      case 'connected':
        return <Wifi className="h-5 w-5 text-green-500" />
      case 'connecting':
        return <Radio className="h-5 w-5 text-yellow-500 animate-pulse" />
      case 'error':
        return <WifiOff className="h-5 w-5 text-red-500" />
      default:
        return <WifiOff className="h-5 w-5 text-gray-500" />
    }
  }

  const getStatusText = () => {
    switch (recording.connectionStatus) {
      case 'connected':
        return 'Connected'
      case 'connecting':
        return 'Connecting...'
      case 'error':
        return 'Connection Error'
      default:
        return 'Disconnected'
    }
  }

  return (
    <>
      {/* Connection Status */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 mb-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            {getStatusIcon()}
            <div>
              <h3 className="font-medium text-gray-900 dark:text-gray-100">
                Backend Connection
              </h3>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                {getStatusText()}
              </p>
            </div>
          </div>
          
          <div className="text-right">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              User: {user?.name || user?.email}
            </p>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Endpoint: /ws?codec=pcm
            </p>
          </div>
        </div>
      </div>

      {/* Component Status Indicators */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-4">📊 Component Status</h3>
        
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {/* WebSocket Status */}
          <div className="text-center">
            <div className={`w-12 h-12 mx-auto mb-2 rounded-full flex items-center justify-center ${
              recording.hasValidWebSocket
                ? 'bg-green-100 text-green-600 dark:bg-green-900 dark:text-green-400'
                : recording.connectionStatus === 'connecting'
                ? 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900 dark:text-yellow-400'
                : recording.connectionStatus === 'error'
                ? 'bg-red-100 text-red-600 dark:bg-red-900 dark:text-red-400'
                : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
            }`}>
              {recording.hasValidWebSocket ? '🔌' : recording.connectionStatus === 'connecting' ? '⏳' : recording.connectionStatus === 'error' ? '❌' : '⚫'}
            </div>
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">WebSocket</p>
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {recording.hasValidWebSocket ? 'Connected' : recording.connectionStatus === 'connecting' ? 'Connecting' : recording.connectionStatus === 'error' ? 'Error' : 'Disconnected'}
            </p>
          </div>

          {/* Microphone Status */}
          <div className="text-center">
            <div className={`w-12 h-12 mx-auto mb-2 rounded-full flex items-center justify-center ${
              recording.hasValidMicrophone || recording.hasMicrophoneAccess
                ? 'bg-green-100 text-green-600 dark:bg-green-900 dark:text-green-400'
                : recording.componentErrors.microphone
                ? 'bg-red-100 text-red-600 dark:bg-red-900 dark:text-red-400'
                : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
            }`}>
              {(recording.hasValidMicrophone || recording.hasMicrophoneAccess) ? '🎤' : recording.componentErrors.microphone ? '❌' : '⚫'}
            </div>
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">Microphone</p>
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {(recording.hasValidMicrophone || recording.hasMicrophoneAccess) ? 'Granted' : recording.componentErrors.microphone ? 'Denied' : 'Unknown'}
            </p>
          </div>

          {/* Audio Context Status */}
          <div className="text-center">
            <div className={`w-12 h-12 mx-auto mb-2 rounded-full flex items-center justify-center ${
              recording.hasValidAudioContext || recording.hasAudioContext
                ? 'bg-green-100 text-green-600 dark:bg-green-900 dark:text-green-400'
                : recording.componentErrors.audioContext
                ? 'bg-red-100 text-red-600 dark:bg-red-900 dark:text-red-400'
                : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
            }`}>
              {(recording.hasValidAudioContext || recording.hasAudioContext) ? '📊' : recording.componentErrors.audioContext ? '❌' : '⚫'}
            </div>
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">Audio Context</p>
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {(recording.hasValidAudioContext || recording.hasAudioContext) ? 'Active' : recording.componentErrors.audioContext ? 'Error' : 'Inactive'}
            </p>
          </div>

          {/* Streaming Status */}
          <div className="text-center">
            <div className={`w-12 h-12 mx-auto mb-2 rounded-full flex items-center justify-center ${
              recording.isCurrentlyStreaming || recording.isStreaming
                ? 'bg-green-100 text-green-600 dark:bg-green-900 dark:text-green-400'
                : recording.componentErrors.streaming
                ? 'bg-red-100 text-red-600 dark:bg-red-900 dark:text-red-400'
                : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
            }`}>
              {(recording.isCurrentlyStreaming || recording.isStreaming) ? '🎵' : recording.componentErrors.streaming ? '❌' : '⚫'}
            </div>
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">Streaming</p>
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {(recording.isCurrentlyStreaming || recording.isStreaming) ? 'Active' : recording.componentErrors.streaming ? 'Error' : 'Inactive'}
            </p>
          </div>
        </div>
      </div>
    </>
  )
}