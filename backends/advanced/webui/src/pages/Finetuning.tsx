import { useState } from 'react'
import { Zap, RefreshCw, AlertCircle, AlertTriangle, CheckCircle, Clock, Play, ToggleLeft, ToggleRight, Edit3, X, Check } from 'lucide-react'
import cronstrue from 'cronstrue'
import { finetuningApi } from '../services/api'
import { useFinetuningStatus, useCronJobs, useToggleCronJob, useUpdateCronSchedule, useRunCronJob, useProcessAnnotations, useDeleteOrphanedAnnotations } from '../hooks/useFinetuning'

interface AnnotationTypeCounts {
  total: number
  pending: number
  applied: number
  trained: number
  orphaned: number
}

function humanCron(expr: string): string {
  try {
    return cronstrue.toString(expr)
  } catch {
    return expr
  }
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return 'Never'
  return new Date(iso).toLocaleString()
}

const JOB_DISPLAY_NAMES: Record<string, string> = {
  speaker_finetuning: 'Speaker Fine-tuning',
  asr_jargon_extraction: 'ASR Jargon Extraction',
}

const ANNOTATION_TYPE_DISPLAY: Record<string, { label: string; description: string }> = {
  diarization: { label: 'Diarization', description: 'Speaker identification corrections' },
  entity: { label: 'Entity', description: 'Knowledge graph entity corrections' },
  transcript: { label: 'Transcript', description: 'Transcript text corrections' },
  memory: { label: 'Memory', description: 'Memory content corrections' },
  title: { label: 'Title', description: 'Conversation title corrections' },
}

function getAnnotationDisplay(key: string): { label: string; description: string } {
  if (ANNOTATION_TYPE_DISPLAY[key]) return ANNOTATION_TYPE_DISPLAY[key]
  const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  return { label, description: `${label} annotations` }
}

const COLOR_CLASSES = {
  blue: 'text-blue-600 dark:text-blue-400',
  green: 'text-green-600 dark:text-green-400',
  default: 'text-gray-900 dark:text-gray-100',
}

function StatCard({ label, value, color, subtitle }: {
  label: string
  value: number
  color?: 'blue' | 'green'
  subtitle?: string
}) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
      <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">{label}</div>
      <div className={`text-2xl font-bold ${color ? COLOR_CLASSES[color] : COLOR_CLASSES.default}`}>
        {value}
      </div>
      {subtitle && <div className="text-xs text-gray-500 mt-1">{subtitle}</div>}
    </div>
  )
}

export default function Finetuning() {
  const { data: status = null, isLoading: statusLoading, error: statusError, refetch: refetchStatus } = useFinetuningStatus()
  const { data: cronJobs = [], isLoading: cronLoading, error: cronError, refetch: refetchCron } = useCronJobs()

  const loading = statusLoading || cronLoading
  const queryError = statusError?.message || cronError?.message || null

  const [error, setError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)
  const [runningJobId, setRunningJobId] = useState<string | null>(null)
  const [showOrphanPanel, setShowOrphanPanel] = useState(false)
  const [cleaningType, setCleaningType] = useState<string | null>(null)
  const [editingSchedule, setEditingSchedule] = useState<string | null>(null)
  const [scheduleInput, setScheduleInput] = useState('')

  const toggleJob = useToggleCronJob()
  const updateSchedule = useUpdateCronSchedule()
  const runJob = useRunCronJob()
  const processAnnotations = useProcessAnnotations()
  const deleteOrphaned = useDeleteOrphanedAnnotations()

  const loadAll = () => {
    refetchStatus()
    refetchCron()
  }

  const displayError = queryError || error

  const handleProcessAnnotations = async () => {
    try {
      setError(null)
      setSuccessMessage(null)
      const data = await processAnnotations.mutateAsync('diarization')
      const totalProcessed = data.total_processed ?? 0
      const failedCount = data.failed_count ?? 0

      if (totalProcessed === 0 && failedCount === 0) {
        setError(data.message || 'No annotations ready for training')
      } else if (totalProcessed === 0 && failedCount > 0) {
        const errorDetail = data.errors?.length ? `: ${data.errors.join(', ')}` : ''
        setError(`All ${failedCount} annotations failed to process${errorDetail}`)
      } else if (failedCount > 0) {
        setSuccessMessage(`Processed ${totalProcessed} annotations (${failedCount} failed)`)
      } else {
        setSuccessMessage(`Successfully processed ${totalProcessed} annotations for training`)
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to process annotations')
    }
  }

  const handleCleanOrphaned = async (annotationType: string) => {
    try {
      setCleaningType(annotationType)
      setError(null)
      setSuccessMessage(null)
      const data = await deleteOrphaned.mutateAsync(annotationType)
      if (data.deleted_count > 0) {
        setSuccessMessage(`Cleaned up ${data.deleted_count} orphaned ${annotationType} annotations`)
      } else {
        setSuccessMessage('No orphaned annotations found')
      }
      setShowOrphanPanel(false)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to clean orphaned annotations')
    } finally {
      setCleaningType(null)
    }
  }

  const handleReattach = async () => {
    try {
      await finetuningApi.reattachOrphanedAnnotations()
    } catch (err: any) {
      const detail = err.response?.data?.detail || 'Reattach functionality coming soon'
      setSuccessMessage(detail)
    }
  }

  const handleToggleJob = async (jobId: string, currentEnabled: boolean) => {
    try {
      setError(null)
      await toggleJob.mutateAsync({ jobId, enabled: !currentEnabled })
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to update job')
    }
  }

  const handleRunNow = async (jobId: string) => {
    try {
      setRunningJobId(jobId)
      setError(null)
      setSuccessMessage(null)
      const data = await runJob.mutateAsync(jobId)
      const jobName = JOB_DISPLAY_NAMES[jobId] || jobId

      if (data.error) {
        setError(`Job '${jobName}' failed: ${data.error}`)
      } else if (data.processed === 0 && data.message) {
        setError(`${jobName}: ${data.message}`)
      } else if (data.processed !== undefined) {
        const parts: string[] = []
        if (data.enrolled) parts.push(`${data.enrolled} new speakers enrolled`)
        if (data.appended) parts.push(`${data.appended} speakers updated`)
        if (data.failed) parts.push(`${data.failed} failed`)
        const detail = parts.length ? ` (${parts.join(', ')})` : ''
        setSuccessMessage(`${jobName}: ${data.processed} annotations processed${detail}`)
      } else {
        setSuccessMessage(`Job '${jobName}' completed successfully`)
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to run job')
    } finally {
      setRunningJobId(null)
    }
  }

  const handleEditSchedule = (jobId: string, currentSchedule: string) => {
    setEditingSchedule(jobId)
    setScheduleInput(currentSchedule)
  }

  const handleSaveSchedule = async (jobId: string) => {
    try {
      setError(null)
      await updateSchedule.mutateAsync({ jobId, schedule: scheduleInput })
      setEditingSchedule(null)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Invalid cron expression')
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600">Loading...</span>
      </div>
    )
  }

  return (
    <div className="max-w-4xl">
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <Zap className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Fine-tuning & Jobs</h1>
        </div>
        <button
          onClick={loadAll}
          className="flex items-center space-x-2 px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
        >
          <RefreshCw className="h-4 w-4" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Error/Success Messages */}
      {displayError && (
        <div className="mb-4 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg flex items-start space-x-2">
          <AlertCircle className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
          <span className="text-red-700 dark:text-red-300">{displayError}</span>
        </div>
      )}

      {successMessage && (
        <div className="mb-4 p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-700 rounded-lg flex items-start space-x-2">
          <CheckCircle className="h-5 w-5 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
          <span className="text-green-700 dark:text-green-300">{successMessage}</span>
        </div>
      )}

      {/* Cron Jobs Section */}
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">Scheduled Jobs</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        {cronJobs.map((job) => (
          <div
            key={job.job_id}
            className="bg-white dark:bg-gray-800 rounded-lg shadow p-5 border border-gray-200 dark:border-gray-700"
          >
            {/* Job Header */}
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
                {JOB_DISPLAY_NAMES[job.job_id] || job.job_id}
              </h3>
              <button
                onClick={() => handleToggleJob(job.job_id, job.enabled)}
                title={job.enabled ? 'Disable' : 'Enable'}
              >
                {job.enabled ? (
                  <ToggleRight className="h-6 w-6 text-green-500" />
                ) : (
                  <ToggleLeft className="h-6 w-6 text-gray-400" />
                )}
              </button>
            </div>

            {/* Description */}
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">{job.description}</p>

            {/* Schedule */}
            <div className="flex items-center space-x-2 mb-2">
              <Clock className="h-4 w-4 text-gray-400 flex-shrink-0" />
              {editingSchedule === job.job_id ? (
                <div className="flex items-center space-x-1 flex-1">
                  <input
                    type="text"
                    value={scheduleInput}
                    onChange={(e) => setScheduleInput(e.target.value)}
                    className="flex-1 text-sm font-mono px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleSaveSchedule(job.job_id)
                      if (e.key === 'Escape') setEditingSchedule(null)
                    }}
                    autoFocus
                  />
                  <button onClick={() => handleSaveSchedule(job.job_id)}>
                    <Check className="h-4 w-4 text-green-500" />
                  </button>
                  <button onClick={() => setEditingSchedule(null)}>
                    <X className="h-4 w-4 text-gray-400" />
                  </button>
                </div>
              ) : (
                <>
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    {humanCron(job.schedule)}
                  </span>
                  <span className="text-xs font-mono text-gray-400">({job.schedule})</span>
                  <button onClick={() => handleEditSchedule(job.job_id, job.schedule)}>
                    <Edit3 className="h-3.5 w-3.5 text-gray-400 hover:text-gray-600" />
                  </button>
                </>
              )}
            </div>

            {/* Last / Next Run */}
            <div className="text-xs text-gray-500 dark:text-gray-400 space-y-1 mb-3">
              <div>Last run: {formatTimestamp(job.last_run)}</div>
              <div>Next run: {formatTimestamp(job.next_run)}</div>
            </div>

            {/* Error */}
            {job.last_error && (
              <div className="text-xs text-red-500 mb-3 truncate" title={job.last_error}>
                Error: {job.last_error}
              </div>
            )}

            {/* Run Now Button */}
            <button
              onClick={() => handleRunNow(job.job_id)}
              disabled={runningJobId === job.job_id || job.running}
              className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            >
              {runningJobId === job.job_id || job.running ? (
                <>
                  <RefreshCw className="h-4 w-4 animate-spin" />
                  <span>Running...</span>
                </>
              ) : (
                <>
                  <Play className="h-4 w-4" />
                  <span>Run Now</span>
                </>
              )}
            </button>
          </div>
        ))}
      </div>

      {/* Annotation Statistics — All Types */}
      {(() => {
        const totalOrphaned = Object.values((status?.annotation_counts || {}) as Record<string, AnnotationTypeCounts>)
          .reduce((sum, c) => sum + (c.orphaned || 0), 0)

        return (
          <div className="mb-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Annotations</h2>
              {totalOrphaned > 0 && (
                <button
                  onClick={() => setShowOrphanPanel(!showOrphanPanel)}
                  className="flex items-center space-x-1.5 px-3 py-1.5 bg-amber-50 dark:bg-amber-900/30 border border-amber-300 dark:border-amber-600 text-amber-700 dark:text-amber-400 rounded-lg hover:bg-amber-100 dark:hover:bg-amber-900/50 transition-colors text-sm"
                >
                  <AlertTriangle className="h-4 w-4" />
                  <span>{totalOrphaned} orphaned</span>
                </button>
              )}
            </div>

            {/* Orphan cleanup panel */}
            {showOrphanPanel && totalOrphaned > 0 && (
              <div className="mt-3 p-4 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-lg">
                <p className="text-sm text-amber-800 dark:text-amber-300 mb-3">
                  These annotations reference conversations that no longer exist.
                </p>
                <div className="space-y-2">
                  {Object.entries((status?.annotation_counts || {}) as Record<string, AnnotationTypeCounts>).map(([key, counts]) => {
                    const orphaned = counts.orphaned || 0
                    if (orphaned === 0) return null
                    const { label } = getAnnotationDisplay(key)
                    return (
                      <div key={key} className="flex items-center justify-between">
                        <span className="text-sm text-gray-700 dark:text-gray-300">
                          {label}: {orphaned} orphaned
                        </span>
                        <div className="flex space-x-2">
                          <button
                            onClick={() => handleCleanOrphaned(key)}
                            disabled={cleaningType === key}
                            className="px-3 py-1 bg-amber-600 text-white text-xs rounded hover:bg-amber-700 disabled:bg-gray-300 transition-colors"
                          >
                            {cleaningType === key ? 'Cleaning...' : 'Clean up'}
                          </button>
                          <button
                            onClick={handleReattach}
                            className="px-3 py-1 bg-gray-200 dark:bg-gray-600 text-gray-500 dark:text-gray-400 text-xs rounded cursor-not-allowed"
                            title="Coming soon"
                          >
                            Reattach
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        )
      })()}
      {status?.annotation_counts && (
        <div className="space-y-6 mb-6">
          {Object.entries(status.annotation_counts! as Record<string, AnnotationTypeCounts>).map(([key, counts]) => {
            const { label, description } = getAnnotationDisplay(key)
            return (
              <div key={key}>
                <div className="flex items-center space-x-2 mb-2">
                  <h3 className="text-base font-medium text-gray-900 dark:text-gray-100">{label}</h3>
                  <span className="text-xs text-gray-500 dark:text-gray-400">{description}</span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <StatCard label="Total" value={counts.total} />
                  <StatCard label="Pending" value={counts.pending} subtitle="Not yet applied" />
                  <StatCard label="Applied" value={counts.applied} color="blue" subtitle="Applied, not trained" />
                  <StatCard label="Trained" value={counts.trained} color="green" subtitle="Sent to model" />
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Fallback if annotation_counts not available */}
      {!status?.annotation_counts && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
          <StatCard label="Pending" value={status?.pending_annotation_count || 0} subtitle="Not yet applied" />
          <StatCard label="Ready for Training" value={status?.applied_annotation_count || 0} color="blue" subtitle="Applied but not trained" />
          <StatCard label="Trained" value={status?.trained_annotation_count || 0} color="green" subtitle="Sent to model" />
        </div>
      )}

      {/* Manual Training Trigger */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">Manual Speaker Training</h2>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          Process applied diarization annotations and send them to the speaker recognition service for model fine-tuning.
        </p>
        <button
          onClick={handleProcessAnnotations}
          disabled={processAnnotations.isPending || (status?.applied_annotation_count || 0) === 0}
          className="flex items-center space-x-2 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
        >
          {processAnnotations.isPending ? (
            <>
              <RefreshCw className="h-5 w-5 animate-spin" />
              <span>Processing...</span>
            </>
          ) : (
            <>
              <Zap className="h-5 w-5" />
              <span>Process {status?.applied_annotation_count || 0} Diarization Annotations</span>
            </>
          )}
        </button>
      </div>
    </div>
  )
}
