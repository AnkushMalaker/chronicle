import { UseAudioRecordingReturn } from '../../hooks/useAudioRecording'
import { Button } from '../ui'

interface AudioRecordingControlsProps {
  recording: UseAudioRecordingReturn
}

export default function AudioRecordingControls({ recording }: AudioRecordingControlsProps) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
      <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-4">🔧 WebSocket Protocol Testing</h3>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {/* WebSocket Connection */}
        <div className="text-center">
          <button
            onClick={recording.isWebSocketConnected ? recording.disconnectWebSocketOnly : recording.connectWebSocketOnly}
            disabled={recording.connectionStatus === 'connecting'}
            className={`w-full px-4 py-2 rounded-lg font-medium transition-colors ${
              recording.isWebSocketConnected
                ? 'bg-red-600 hover:bg-red-700 text-white'
                : 'bg-green-600 hover:bg-green-700 text-white'
            } disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            {recording.isWebSocketConnected ? '🔌 Disconnect' : '🔗 Connect'}
          </button>
          <p className="text-xs text-gray-500 mt-1">WebSocket</p>
        </div>

        {/* Audio Start */}
        <div className="text-center">
          <Button
            variant="primary"
            size="md"
            onClick={recording.sendAudioStartOnly}
            disabled={!recording.hasValidWebSocket}
            className="w-full"
          >
            📤 Start
          </Button>
          <p className="text-xs text-gray-500 mt-1">Send audio-start</p>
        </div>

        {/* Audio Stop */}
        <div className="text-center">
          <button
            onClick={recording.sendAudioStopOnly}
            disabled={!recording.hasValidWebSocket}
            className="w-full px-4 py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            📤 Stop
          </button>
          <p className="text-xs text-gray-500 mt-1">Send audio-stop</p>
        </div>

        {/* Full Recording (Original) */}
        <div className="text-center">
          <button
            onClick={recording.isRecording ? recording.stopRecording : recording.startRecording}
            disabled={!recording.canAccessMicrophone || recording.connectionStatus === 'connecting'}
            className={`w-full px-4 py-2 rounded-lg font-medium transition-colors ${
              recording.isRecording
                ? 'bg-red-600 hover:bg-red-700 text-white'
                : 'bg-purple-600 hover:bg-purple-700 text-white'
            } disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            {recording.isRecording ? '🛑 Stop Process' : '🎵 Process Audio'}
          </button>
          <p className="text-xs text-gray-500 mt-1">Complete processing</p>
        </div>
      </div>

      {/* New Granular Testing Controls */}
      <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-4 mt-6">🧪 Granular Component Testing</h3>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {/* Microphone Access Test */}
        <div className="text-center">
          <button
            onClick={recording.requestMicrophoneOnly}
            disabled={!recording.canAccessMicrophone}
            className={`w-full px-4 py-2 rounded-lg font-medium transition-colors ${
              recording.hasMicrophoneAccess
                ? 'bg-green-600 hover:bg-green-700 text-white'
                : 'bg-yellow-600 hover:bg-yellow-700 text-white'
            } disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            {recording.hasMicrophoneAccess ? '🎤 ✓ Mic OK' : '🎤 Get Mic'}
          </button>
          <p className="text-xs text-gray-500 mt-1">Microphone access</p>
          {recording.componentErrors.microphone && (
            <p className="text-xs text-red-500 mt-1">{recording.componentErrors.microphone}</p>
          )}
        </div>

        {/* Audio Context Test */}
        <div className="text-center">
          <button
            onClick={recording.createAudioContextOnly}
            className={`w-full px-4 py-2 rounded-lg font-medium transition-colors ${
              recording.hasAudioContext
                ? 'bg-green-600 hover:bg-green-700 text-white'
                : 'bg-indigo-600 hover:bg-indigo-700 text-white'
            }`}
          >
            {recording.hasAudioContext ? '📊 ✓ Context OK' : '📊 Create Context'}
          </button>
          <p className="text-xs text-gray-500 mt-1">Audio context</p>
          {recording.componentErrors.audioContext && (
            <p className="text-xs text-red-500 mt-1">{recording.componentErrors.audioContext}</p>
          )}
        </div>

        {/* Audio Streaming Test */}
        <div className="text-center">
          <button
            onClick={recording.isStreaming ? recording.stopStreamingOnly : recording.startStreamingOnly}
            disabled={!recording.hasValidWebSocket || !recording.hasMicrophoneAccess || !recording.hasAudioContext}
            className={`w-full px-4 py-2 rounded-lg font-medium transition-colors ${
              recording.isStreaming
                ? 'bg-red-600 hover:bg-red-700 text-white'
                : 'bg-teal-600 hover:bg-teal-700 text-white'
            } disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            {recording.isStreaming ? '🎵 Stop Stream' : '🎵 Start Stream'}
          </button>
          <p className="text-xs text-gray-500 mt-1">Audio streaming</p>
          {recording.componentErrors.streaming && (
            <p className="text-xs text-red-500 mt-1">{recording.componentErrors.streaming}</p>
          )}
        </div>

        {/* Full Flow Test */}
        <div className="text-center">
          <button
            onClick={recording.testFullFlowOnly}
            disabled={!recording.canAccessMicrophone}
            className="w-full px-4 py-2 bg-cyan-600 hover:bg-cyan-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            💾 Test Flow
          </button>
          <p className="text-xs text-gray-500 mt-1">10s full test</p>
        </div>
      </div>
    </div>
  )
}
