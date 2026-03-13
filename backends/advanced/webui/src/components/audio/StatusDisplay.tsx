import React from 'react'
import { Check, Loader2, AlertCircle, Mic, Wifi, Play, Radio } from 'lucide-react'
import { RecordingContextType, RecordingStep } from '../../contexts/RecordingContext'

interface StatusDisplayProps {
  recording: RecordingContextType
}

interface StepInfo {
  id: RecordingStep
  label: string
  icon: React.ReactNode
  description: string
}

const steps: StepInfo[] = [
  {
    id: 'mic',
    label: 'Microphone',
    icon: <Mic className="h-4 w-4" />,
    description: 'Request microphone access'
  },
  {
    id: 'websocket',
    label: 'Connection',
    icon: <Wifi className="h-4 w-4" />,
    description: 'Connect to backend server'
  },
  {
    id: 'audio-start',
    label: 'Initialize',
    icon: <Play className="h-4 w-4" />,
    description: 'Start audio session'
  },
  {
    id: 'streaming',
    label: 'Streaming',
    icon: <Radio className="h-4 w-4" />,
    description: 'Stream audio data'
  }
]

const getStepStatus = (stepId: RecordingStep, currentStep: RecordingStep, isRecording: boolean): 'pending' | 'current' | 'completed' | 'error' => {
  if (currentStep === 'error') {
    // Find which step we were on when error occurred
    const stepIndex = steps.findIndex(s => s.id === stepId)
    const currentStepIndex = steps.findIndex(s => s.id === currentStep)
    if (stepIndex <= currentStepIndex) return 'error'
    return 'pending'
  }

  if (isRecording) {
    return 'completed' // All steps completed when recording
  }

  const stepIndex = steps.findIndex(s => s.id === stepId)
  const currentStepIndex = steps.findIndex(s => s.id === currentStep)

  if (stepIndex < currentStepIndex) return 'completed'
  if (stepIndex === currentStepIndex) return 'current'
  return 'pending'
}

const getStatusIcon = (status: string) => {
  switch (status) {
    case 'completed': return <Check className="h-4 w-4 text-green-600" />
    case 'current': return <Loader2 className="h-4 w-4 text-blue-600 animate-spin" />
    case 'error': return <AlertCircle className="h-4 w-4 text-red-600" />
    default: return <div className="h-4 w-4 rounded-full bg-gray-300" />
  }
}

const getStatusColor = (status: string): string => {
  switch (status) {
    case 'completed': return 'border-green-600 bg-green-50 dark:bg-green-900/20'
    case 'current': return 'border-blue-600 bg-blue-50 dark:bg-blue-900/20'
    case 'error': return 'border-red-600 bg-red-50 dark:bg-red-900/20'
    default: return 'border-gray-300 bg-gray-50 dark:bg-gray-800'
  }
}

export default function StatusDisplay({ recording }: StatusDisplayProps) {
  // Don't show status display when idle or recording (keep it clean)
  if (recording.currentStep === 'idle' || recording.isRecording) {
    return null
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
      <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Radio className="h-5 w-5 mr-2 text-blue-600" />
        Recording Setup Progress
      </h3>

      <div className="space-y-3">
        {steps.map((step, index) => {
          const status = getStepStatus(step.id, recording.currentStep, recording.isRecording)

          return (
            <div
              key={step.id}
              className={`flex items-center p-3 rounded-lg border-2 transition-colors ${getStatusColor(status)}`}
            >
              {/* Step Icon */}
              <div className="flex items-center justify-center w-8 h-8 rounded-full bg-white dark:bg-gray-700 mr-3">
                {step.icon}
              </div>

              {/* Step Info */}
              <div className="flex-1">
                <div className="flex items-center justify-between">
                  <h4 className="font-medium text-gray-900 dark:text-gray-100">
                    {step.label}
                  </h4>
                  {getStatusIcon(status)}
                </div>
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  {step.description}
                </p>
              </div>

              {/* Step Number */}
              <div className="ml-3 text-xs text-gray-500 font-mono">
                {index + 1}
              </div>
            </div>
          )
        })}
      </div>

      {/* Overall Status */}
      <div className="mt-4 p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
        <div className="flex items-center justify-between text-sm">
          <span className="text-gray-600 dark:text-gray-400">
            {recording.currentStep === 'error' ? 'Setup failed' : 'Setting up recording...'}
          </span>
          <span className="font-mono text-gray-500">
            {steps.findIndex(s => s.id === recording.currentStep) + 1}/{steps.length}
          </span>
        </div>
      </div>
    </div>
  )
}
