import { useState, useEffect } from 'react'
import { Zap, RefreshCw, AlertCircle, CheckCircle, Clock } from 'lucide-react'
import { finetuningApi } from '../services/api'

interface FinetuningStatus {
  pending_annotation_count: number
  applied_annotation_count: number
  trained_annotation_count: number
  last_training_run: string | null
  cron_status: {
    enabled: boolean
    schedule: string
    last_run: string | null
    next_run: string | null
  }
}

export default function Finetuning() {
  const [status, setStatus] = useState<FinetuningStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [processing, setProcessing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)

  useEffect(() => {
    loadStatus()
  }, [])

  const loadStatus = async () => {
    try {
      setLoading(true)
      const response = await finetuningApi.getStatus()
      setStatus(response.data)
      setError(null)
    } catch (err: any) {
      setError(err.message || 'Failed to load fine-tuning status')
    } finally {
      setLoading(false)
    }
  }

  const handleProcessAnnotations = async () => {
    try {
      setProcessing(true)
      setError(null)
      setSuccessMessage(null)

      const response = await finetuningApi.processAnnotations('diarization')

      setSuccessMessage(
        `Successfully processed ${response.data.processed_count} annotations for training`
      )

      // Reload status
      await loadStatus()
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to process annotations')
    } finally {
      setProcessing(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600">Loading fine-tuning status...</span>
      </div>
    )
  }

  return (
    <div className="max-w-4xl">
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <Zap className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Model Fine-tuning</h1>
        </div>
        <button
          onClick={loadStatus}
          className="flex items-center space-x-2 px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
        >
          <RefreshCw className="h-4 w-4" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Error/Success Messages */}
      {error && (
        <div className="mb-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg flex items-start space-x-2">
          <AlertCircle className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
          <span className="text-red-700 dark:text-red-300">{error}</span>
        </div>
      )}

      {successMessage && (
        <div className="mb-4 p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-700 rounded-lg flex items-start space-x-2">
          <CheckCircle className="h-5 w-5 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
          <span className="text-green-700 dark:text-green-300">{successMessage}</span>
        </div>
      )}

      {/* Annotation Statistics */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
          <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">Pending Annotations</div>
          <div className="text-3xl font-bold text-gray-900 dark:text-gray-100">
            {status?.pending_annotation_count || 0}
          </div>
          <div className="text-xs text-gray-500 mt-1">Not yet applied</div>
        </div>

        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
          <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">Ready for Training</div>
          <div className="text-3xl font-bold text-blue-600 dark:text-blue-400">
            {status?.applied_annotation_count || 0}
          </div>
          <div className="text-xs text-gray-500 mt-1">Applied but not trained</div>
        </div>

        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
          <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">Trained</div>
          <div className="text-3xl font-bold text-green-600 dark:text-green-400">
            {status?.trained_annotation_count || 0}
          </div>
          <div className="text-xs text-gray-500 mt-1">Sent to model</div>
        </div>
      </div>

      {/* Manual Training Trigger */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 mb-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">Manual Training</h2>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          Process applied annotations and send them to the speaker recognition service for model fine-tuning.
          This will improve speaker identification based on your corrections.
        </p>
        <button
          onClick={handleProcessAnnotations}
          disabled={processing || (status?.applied_annotation_count || 0) === 0}
          className="flex items-center space-x-2 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
        >
          {processing ? (
            <>
              <RefreshCw className="h-5 w-5 animate-spin" />
              <span>Processing...</span>
            </>
          ) : (
            <>
              <Zap className="h-5 w-5" />
              <span>Process {status?.applied_annotation_count || 0} Annotations</span>
            </>
          )}
        </button>
      </div>

      {/* Cron Job Status */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">Automated Training</h2>
        <div className="space-y-3">
          <div className="flex items-center space-x-2">
            <span className="text-sm text-gray-600 dark:text-gray-400">Status:</span>
            <span className={`text-sm font-medium ${
              status?.cron_status.enabled ? 'text-green-600 dark:text-green-400' : 'text-gray-400'
            }`}>
              {status?.cron_status.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          <div className="flex items-center space-x-2">
            <Clock className="h-4 w-4 text-gray-400" />
            <span className="text-sm text-gray-600 dark:text-gray-400">Schedule:</span>
            <span className="text-sm font-mono text-gray-900 dark:text-gray-100">
              {status?.cron_status.schedule || 'Not configured'}
            </span>
          </div>
          {status?.cron_status.last_run && (
            <div className="flex items-center space-x-2">
              <span className="text-sm text-gray-600 dark:text-gray-400">Last Run:</span>
              <span className="text-sm text-gray-900 dark:text-gray-100">
                {new Date(status.cron_status.last_run).toLocaleString()}
              </span>
            </div>
          )}
          {status?.cron_status.next_run && (
            <div className="flex items-center space-x-2">
              <span className="text-sm text-gray-600 dark:text-gray-400">Next Run:</span>
              <span className="text-sm text-gray-900 dark:text-gray-100">
                {new Date(status.cron_status.next_run).toLocaleString()}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
