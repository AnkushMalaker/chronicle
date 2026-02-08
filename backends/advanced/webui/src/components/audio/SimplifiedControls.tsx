import { Mic, Square, Loader2 } from 'lucide-react'
import { RecordingContextType } from '../../contexts/RecordingContext'

interface SimplifiedControlsProps {
  recording: RecordingContextType
}

const getStepText = (step: string): string => {
  switch (step) {
    case 'idle': return 'Ready to Record'
    case 'mic': return 'Getting Microphone Access...'
    case 'websocket': return 'Connecting to Server...'
    case 'audio-start': return 'Initializing Audio Session...'
    case 'streaming': return 'Starting Audio Stream...'
    case 'stopping': return 'Stopping Recording...'
    case 'error': return 'Error Occurred'
    default: return 'Processing...'
  }
}

const isProcessing = (step: string): boolean => {
  return ['mic', 'websocket', 'audio-start', 'streaming', 'stopping'].includes(step)
}

export default function SimplifiedControls({ recording }: SimplifiedControlsProps) {
  const processing = isProcessing(recording.currentStep)
  const canStart = recording.canAccessMicrophone && !processing && !recording.isRecording

  const handleClick = () => {
    if (recording.isRecording) {
      recording.stopRecording()
    } else if (canStart) {
      recording.startRecording()
    }
  }

  // Button appearance based on state
  const getButtonClasses = (): string => {
    if (recording.isRecording) return 'bg-red-600 hover:bg-red-700'
    if (processing) return 'bg-yellow-600'
    if (recording.currentStep === 'error') return 'bg-red-600 hover:bg-red-700'
    return 'bg-blue-600 hover:bg-blue-700'
  }

  const isDisabled = recording.isRecording ? false : (processing || !canStart)

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-8 mb-6">
      <div className="text-center">
        {/* Single Toggle Button */}
        <div className="mb-6 flex justify-center">
          <div className="relative">
            {/* Pulsing ring when recording */}
            {recording.isRecording && (
              <span className="absolute inset-0 rounded-full bg-red-400 opacity-30 animate-ping" />
            )}
            <button
              onClick={handleClick}
              disabled={isDisabled}
              className={`relative w-24 h-24 ${getButtonClasses()} text-white rounded-full flex items-center justify-center transition-all duration-200 shadow-lg disabled:opacity-50 disabled:cursor-not-allowed transform hover:scale-105 active:scale-95`}
            >
              {recording.isRecording ? (
                <Square className="h-10 w-10 fill-current" />
              ) : processing ? (
                <Loader2 className="h-10 w-10 animate-spin" />
              ) : (
                <Mic className="h-10 w-10" />
              )}
            </button>
          </div>
        </div>

        {/* Status Text */}
        <div className="space-y-2">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
            {recording.isRecording ? 'Recording in Progress' : getStepText(recording.currentStep)}
          </h2>

          {/* Recording Duration */}
          {recording.isRecording && (
            <p className="text-3xl font-mono text-red-600 dark:text-red-400">
              {recording.formatDuration(recording.recordingDuration)}
            </p>
          )}

          {/* Action Text */}
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {recording.isRecording
              ? 'Click to stop recording'
              : recording.currentStep === 'idle'
                ? 'Click to start recording'
                : recording.currentStep === 'error'
                  ? 'Click to try again'
                  : 'Please wait while setting up...'}
          </p>

          {/* Error Message */}
          {recording.error && (
            <div className="mt-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
              <p className="text-sm text-red-700 dark:text-red-300">
                <strong>Error:</strong> {recording.error}
              </p>
            </div>
          )}

          {/* Security Warning */}
          {!recording.canAccessMicrophone && (
            <div className="mt-4 p-3 bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-800 rounded-lg">
              <p className="text-sm text-orange-700 dark:text-orange-300">
                <strong>Secure Access Required:</strong> Microphone access requires HTTPS or localhost
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
