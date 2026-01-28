import { Mic, MicOff, Loader2 } from 'lucide-react'
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

const getButtonColor = (step: string, isRecording: boolean): string => {
  if (step === 'error') return 'bg-red-600 hover:bg-red-700'
  if (isRecording) return 'bg-red-600 hover:bg-red-700'
  if (step === 'idle') return 'bg-blue-600 hover:bg-blue-700'
  return 'bg-yellow-600 hover:bg-yellow-700'
}

const isProcessing = (step: string): boolean => {
  return ['mic', 'websocket', 'audio-start', 'streaming', 'stopping'].includes(step)
}

export default function SimplifiedControls({ recording }: SimplifiedControlsProps) {
  const startButtonDisabled = !recording.canAccessMicrophone || isProcessing(recording.currentStep) || recording.isRecording
  
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-8 mb-6">
      <div className="text-center">
        {/* Control Buttons */}
        <div className="mb-6 flex justify-center space-x-4">
          {/* START Button */}
          <button
            onClick={recording.startRecording}
            disabled={startButtonDisabled}
            className={`w-24 h-24 ${recording.isRecording || isProcessing(recording.currentStep) ? 'bg-gray-400' : getButtonColor(recording.currentStep, recording.isRecording)} text-white rounded-full flex items-center justify-center transition-all duration-200 shadow-lg disabled:opacity-50 disabled:cursor-not-allowed transform hover:scale-105 active:scale-95`}
          >
            {isProcessing(recording.currentStep) ? (
              <Loader2 className="h-10 w-10 animate-spin" />
            ) : (
              <Mic className="h-10 w-10" />
            )}
          </button>
          
          {/* STOP Button - only show when recording */}
          {recording.isRecording && (
            <button
              onClick={recording.stopRecording}
              className="w-24 h-24 bg-red-600 hover:bg-red-700 text-white rounded-full flex items-center justify-center transition-all duration-200 shadow-lg transform hover:scale-105 active:scale-95"
            >
              <MicOff className="h-10 w-10" />
            </button>
          )}
        </div>
        
        {/* Status Text */}
        <div className="space-y-2">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
            {recording.isRecording ? 'Recording in Progress' : getStepText(recording.currentStep)}
          </h2>
          
          {/* Recording Duration */}
          {recording.isRecording && (
            <p className="text-3xl font-mono text-blue-600 dark:text-blue-400">
              {recording.formatDuration(recording.recordingDuration)}
            </p>
          )}
          
          {/* Action Text */}
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {recording.isRecording 
              ? 'Click the red STOP button to end recording'
              : recording.currentStep === 'idle' 
                ? 'Click the blue START button to begin recording'
                : recording.currentStep === 'error'
                  ? 'Click START to try again'
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