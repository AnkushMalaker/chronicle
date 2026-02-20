import { useState, useEffect, useRef, useCallback } from 'react'
import { Activity, RefreshCw, CheckCircle, XCircle, AlertCircle, Users, Database, Server, MoreVertical, RotateCcw, Power } from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useSystemData, useRestartWorkers, useRestartBackend } from '../hooks/useSystem'
import { systemApi } from '../services/api'

interface ServiceStatus {
  healthy: boolean
  message?: string
  status?: string
}

export default function System() {
  const { isAdmin } = useAuth()

  // TanStack Query hooks for data fetching
  const { data: systemData, isLoading: loading, error: systemError, refetch: refetchSystem, dataUpdatedAt } = useSystemData(isAdmin)

  // Restart mutations
  const restartWorkersMutation = useRestartWorkers()
  const restartBackendMutation = useRestartBackend()

  // UI state
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmModal, setConfirmModal] = useState<'workers' | 'backend' | 'both' | null>(null)
  const [restartingBackend, setRestartingBackend] = useState(false)
  const [workerBanner, setWorkerBanner] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Derive state from query results
  const healthData = systemData?.healthData ?? null
  const readinessData = systemData?.readinessData ?? null
  const metricsData = systemData?.metricsData ?? null
  const configDiagnostics = systemData?.configDiagnostics ?? null
  const processorStatus = systemData?.processorStatus ?? null
  const activeClients = systemData?.activeClients ?? []
  const error = systemError?.message ?? null
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null

  const loadSystemData = () => refetchSystem()

  // Close menu on click outside
  useEffect(() => {
    if (!menuOpen) return
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [menuOpen])

  // Close modal on ESC
  useEffect(() => {
    if (!confirmModal) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setConfirmModal(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [confirmModal])

  // Poll health during backend restart
  const pollHealth = useCallback(async () => {
    setRestartingBackend(true)
    // Wait for the backend to actually go down
    await new Promise(r => setTimeout(r, 3000))

    let attempts = 0
    const maxAttempts = 60
    const poll = async () => {
      while (attempts < maxAttempts) {
        attempts++
        try {
          await systemApi.getHealth()
          // Backend is back
          setRestartingBackend(false)
          refetchSystem()
          return
        } catch {
          // Still down, wait and retry
          await new Promise(r => setTimeout(r, 2000))
        }
      }
      // Timed out
      setRestartingBackend(false)
    }
    await poll()
  }, [refetchSystem])

  const handleRestartWorkers = () => {
    setConfirmModal(null)
    restartWorkersMutation.mutate(undefined, {
      onSuccess: () => {
        setWorkerBanner(true)
        setTimeout(() => setWorkerBanner(false), 8000)
      },
    })
  }

  const handleRestartBackend = () => {
    setConfirmModal(null)
    restartBackendMutation.mutate(undefined, {
      onSuccess: () => {
        pollHealth()
      },
    })
  }

  const handleRestartBoth = () => {
    setConfirmModal(null)
    restartWorkersMutation.mutate(undefined, {
      onSuccess: () => {
        restartBackendMutation.mutate(undefined, {
          onSuccess: () => {
            pollHealth()
          },
        })
      },
    })
  }

  const getStatusIcon = (healthy: boolean) => {
    return healthy
      ? <CheckCircle className="h-5 w-5 text-green-500" />
      : <XCircle className="h-5 w-5 text-red-500" />
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'healthy': return 'text-green-600'
      case 'partial': return 'text-yellow-600'
      default: return 'text-red-600'
    }
  }

  const getServiceDisplayName = (service: string) => {
    const displayNames: Record<string, string> = {
      'mongodb': 'MONGODB',
      'redis': 'REDIS & RQ WORKERS',
      'audioai': 'AUDIOAI',
      'mem0': 'MEM0',
      'memory_service': 'MEMORY SERVICE',
      'speech_to_text': 'SPEECH TO TEXT',
      'speaker_recognition': 'SPEAKER RECOGNITION',
      'openmemory_mcp': 'OPENMEMORY MCP'
    }
    return displayNames[service] || service.replace('_', ' ').toUpperCase()
  }

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString()
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <Activity className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view system status.
        </p>
      </div>
    )
  }

  // Count diagnostics for the collapsible summary
  const issueCount = configDiagnostics?.issues?.length ?? 0
  const warningCount = configDiagnostics?.warnings?.length ?? 0
  const infoCount = configDiagnostics?.info?.length ?? 0
  const totalDiagnostics = issueCount + warningCount + infoCount

  return (
    <div>
      {/* Backend Restarting Overlay */}
      {restartingBackend && (
        <div className="fixed inset-0 z-50 bg-gray-900/80 flex items-center justify-center">
          <div className="text-center">
            <RefreshCw className="h-12 w-12 text-blue-400 animate-spin mx-auto mb-4" />
            <h2 className="text-xl font-semibold text-white mb-2">
              Backend Restarting
            </h2>
            <p className="text-gray-300 text-sm">
              Waiting for the service to come back online...
            </p>
          </div>
        </div>
      )}

      {/* Worker Restart Success Banner */}
      {workerBanner && (
        <div className="mb-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md p-3 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <CheckCircle className="h-5 w-5 text-green-500" />
            <span className="text-sm text-green-700 dark:text-green-300">
              Worker restart signal sent. Workers will restart after finishing current jobs.
            </span>
          </div>
          <button onClick={() => setWorkerBanner(false)} className="text-green-500 hover:text-green-700">
            <XCircle className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <Activity className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            System Status
          </h1>
        </div>
        <div className="flex items-center space-x-4">
          {lastUpdated && (
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Last updated: {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <button
            onClick={loadSystemData}
            disabled={loading}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>

          {/* Three-dot menu */}
          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setMenuOpen(prev => !prev)}
              className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
              title="System actions"
            >
              <MoreVertical className="h-5 w-5 text-gray-600 dark:text-gray-400" />
            </button>
            {menuOpen && (
              <div className="absolute right-0 mt-1 w-52 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-20 py-1">
                <button
                  onClick={() => { setMenuOpen(false); setConfirmModal('workers') }}
                  className="w-full flex items-center px-4 py-2 text-sm text-blue-600 dark:text-blue-400 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  <RotateCcw className="h-4 w-4 mr-2" />
                  Restart Workers
                </button>
                <button
                  onClick={() => { setMenuOpen(false); setConfirmModal('backend') }}
                  className="w-full flex items-center px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  <Power className="h-4 w-4 mr-2" />
                  Restart Backend
                </button>
                <div className="border-t border-gray-200 dark:border-gray-700 my-1" />
                <button
                  onClick={() => { setMenuOpen(false); setConfirmModal('both') }}
                  className="w-full flex items-center px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Restart Both
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Confirmation Modals */}
      {confirmModal && (
        <div className="fixed inset-0 z-40 bg-black/50 flex items-center justify-center" onClick={() => setConfirmModal(null)}>
          <div
            className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full mx-4 p-6"
            onClick={e => e.stopPropagation()}
          >
            {confirmModal === 'workers' ? (
              <>
                <div className="flex items-center space-x-3 mb-4">
                  <div className="p-2 rounded-full bg-blue-100 dark:bg-blue-900/30">
                    <RotateCcw className="h-5 w-5 text-blue-600 dark:text-blue-400" />
                  </div>
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                    Restart Workers
                  </h3>
                </div>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                  Workers will finish their current jobs before restarting. This is safe to run at any time.
                </p>
                <p className="text-sm text-gray-500 dark:text-gray-500 mb-6">
                  Use this after changing plugin configuration or config.yml settings.
                </p>
                <div className="flex justify-end space-x-3">
                  <button
                    onClick={() => setConfirmModal(null)}
                    className="px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleRestartWorkers}
                    className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                  >
                    Restart Workers
                  </button>
                </div>
              </>
            ) : confirmModal === 'backend' ? (
              <>
                <div className="flex items-center space-x-3 mb-4">
                  <div className="p-2 rounded-full bg-red-100 dark:bg-red-900/30">
                    <Power className="h-5 w-5 text-red-600 dark:text-red-400" />
                  </div>
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                    Restart Backend
                  </h3>
                </div>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                  This will restart the entire backend process. The service will be briefly unavailable.
                </p>
                <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3 mb-6">
                  <p className="text-sm text-red-700 dark:text-red-300">
                    Active WebSocket connections and streaming sessions will be dropped.
                  </p>
                </div>
                <div className="flex justify-end space-x-3">
                  <button
                    onClick={() => setConfirmModal(null)}
                    className="px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleRestartBackend}
                    className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors"
                  >
                    Restart Backend
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="flex items-center space-x-3 mb-4">
                  <div className="p-2 rounded-full bg-red-100 dark:bg-red-900/30">
                    <RefreshCw className="h-5 w-5 text-red-600 dark:text-red-400" />
                  </div>
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                    Restart Both
                  </h3>
                </div>
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                  This will restart workers and then the backend. The service will be briefly unavailable.
                </p>
                <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3 mb-6">
                  <p className="text-sm text-red-700 dark:text-red-300">
                    Active WebSocket connections and streaming sessions will be dropped.
                  </p>
                </div>
                <div className="flex justify-end space-x-3">
                  <button
                    onClick={() => setConfirmModal(null)}
                    className="px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleRestartBoth}
                    className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors"
                  >
                    Restart Both
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-4 mb-6">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      {/* Overall Health Status */}
      {healthData && (
        <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-6 border border-gray-200 dark:border-gray-600 mb-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <Activity className="h-6 w-6 text-blue-600" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                System Health
              </h2>
            </div>
            <div className="flex items-center space-x-2">
              {healthData.status === 'healthy' && <CheckCircle className="h-6 w-6 text-green-500" />}
              {healthData.status === 'partial' && <AlertCircle className="h-6 w-6 text-yellow-500" />}
              {healthData.status === 'unhealthy' && <XCircle className="h-6 w-6 text-red-500" />}
              <span className={`font-semibold ${getStatusColor(healthData.status)}`}>
                {healthData.status.toUpperCase()}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Configuration Diagnostics (collapsible) */}
      {configDiagnostics && totalDiagnostics > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <details>
            <summary className="cursor-pointer flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
                <AlertCircle className="h-5 w-5 mr-2 text-blue-600" />
                Configuration Diagnostics
              </h3>
              <div className="flex items-center space-x-3">
                <span className="text-sm text-gray-600 dark:text-gray-400">
                  {issueCount > 0 && `${issueCount} issue${issueCount !== 1 ? 's' : ''}`}
                  {issueCount > 0 && warningCount > 0 && ', '}
                  {warningCount > 0 && `${warningCount} warning${warningCount !== 1 ? 's' : ''}`}
                  {(issueCount > 0 || warningCount > 0) && infoCount > 0 && ', '}
                  {infoCount > 0 && `${infoCount} info`}
                </span>
                {configDiagnostics.overall_status === 'healthy' && <CheckCircle className="h-5 w-5 text-green-500" />}
                {configDiagnostics.overall_status === 'partial' && <AlertCircle className="h-5 w-5 text-yellow-500" />}
                {configDiagnostics.overall_status === 'unhealthy' && <XCircle className="h-5 w-5 text-red-500" />}
              </div>
            </summary>

            <div className="space-y-3 mt-4">
              {/* Errors */}
              {configDiagnostics.issues.map((issue: any, idx: number) => (
                <div key={`error-${idx}`} className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <XCircle className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <span className="text-xs font-semibold text-red-700 dark:text-red-300 uppercase">
                          {issue.component}
                        </span>
                        <span className="text-xs px-2 py-0.5 bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200 rounded">
                          ERROR
                        </span>
                      </div>
                      <p className="text-sm text-red-700 dark:text-red-300 mb-1">
                        {issue.message}
                      </p>
                      {issue.resolution && (
                        <p className="text-xs text-red-600 dark:text-red-400">
                          {issue.resolution}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {/* Warnings */}
              {configDiagnostics.warnings.map((warning: any, idx: number) => (
                <div key={`warning-${idx}`} className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <AlertCircle className="h-5 w-5 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <span className="text-xs font-semibold text-yellow-700 dark:text-yellow-300 uppercase">
                          {warning.component}
                        </span>
                        <span className="text-xs px-2 py-0.5 bg-yellow-200 dark:bg-yellow-800 text-yellow-800 dark:text-yellow-200 rounded">
                          WARNING
                        </span>
                      </div>
                      <p className="text-sm text-yellow-700 dark:text-yellow-300 mb-1">
                        {warning.message}
                      </p>
                      {warning.resolution && (
                        <p className="text-xs text-yellow-600 dark:text-yellow-400">
                          {warning.resolution}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {/* Info */}
              {configDiagnostics.info.map((info: any, idx: number) => (
                <div key={`info-${idx}`} className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <CheckCircle className="h-5 w-5 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <span className="text-xs font-semibold text-blue-700 dark:text-blue-300 uppercase">
                          {info.component}
                        </span>
                        <span className="text-xs px-2 py-0.5 bg-blue-200 dark:bg-blue-800 text-blue-800 dark:text-blue-200 rounded">
                          INFO
                        </span>
                      </div>
                      <p className="text-sm text-blue-700 dark:text-blue-300">
                        {info.message}
                      </p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </details>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Services Status */}
        {healthData?.services && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
              <Database className="h-5 w-5 mr-2 text-blue-600" />
              Services Status
            </h3>
            <div className="space-y-3">
              {Object.entries(healthData.services as Record<string, ServiceStatus>).map(([service, status]) => (
                <div key={service} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div className="flex items-center space-x-3">
                    {getStatusIcon(status.healthy)}
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {getServiceDisplayName(service)}
                    </span>
                  </div>
                  <div className="text-right">
                    {status.message && (
                      <span className="text-sm text-gray-600 dark:text-gray-400 block">
                        {status.message}
                      </span>
                    )}
                    {(status as any).status && (
                      <span className="text-xs text-gray-500 dark:text-gray-500">
                        {(status as any).status}
                      </span>
                    )}
                    {(status as any).provider && (
                      <span className="text-xs text-blue-600 dark:text-blue-400">
                        ({(status as any).provider})
                      </span>
                    )}
                    {service === 'redis' && (status as any).worker_count !== undefined && (
                      <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                        Workers: {(status as any).worker_count} total
                        ({(status as any).active_workers || 0} active, {(status as any).idle_workers || 0} idle)
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Processor Status */}
        {processorStatus && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
              <Server className="h-5 w-5 mr-2 text-blue-600" />
              Processor Status
            </h3>
            <div className="grid grid-cols-2 gap-4 mb-6">
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Audio Queue</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.audio_queue_size}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Transcription Queue</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.transcription_queue_size}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Memory Queue</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.memory_queue_size}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Active Tasks</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.active_tasks}
                </div>
              </div>
            </div>

            {/* Worker Information */}
            {(processorStatus as any).workers && (
              <div className="mt-4 border-t border-gray-200 dark:border-gray-600 pt-4">
                <div className="flex items-center justify-between mb-3">
                  <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">
                    RQ Workers
                  </h4>
                  <span className="text-sm text-gray-600 dark:text-gray-400">
                    {(processorStatus as any).workers.active} / {(processorStatus as any).workers.total} active
                  </span>
                </div>
                <div className="space-y-2">
                  {(processorStatus as any).workers.details?.map((worker: any, idx: number) => (
                    <div key={idx} className="flex items-center justify-between bg-gray-50 dark:bg-gray-700 rounded px-3 py-2 text-sm">
                      <div className="flex items-center space-x-3">
                        <span className={`w-2 h-2 rounded-full ${worker.state === 'idle' ? 'bg-green-500' : 'bg-blue-500'}`}></span>
                        <div className="flex flex-col">
                          <span className="text-gray-900 dark:text-gray-100 font-medium">RQ Worker #{idx + 1}</span>
                          <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">{worker.name.substring(0, 8)}...</span>
                        </div>
                      </div>
                      <div className="flex items-center space-x-3">
                        <span className="text-gray-600 dark:text-gray-400 text-xs">{worker.queues?.join(', ')}</span>
                        <span className={`px-2 py-0.5 rounded text-xs ${
                          worker.state === 'idle'
                            ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                            : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
                        }`}>
                          {worker.state}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Active Clients */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Users className="h-5 w-5 mr-2 text-blue-600" />
            Active Clients ({activeClients.length})
          </h3>
          {activeClients.length > 0 ? (
            <div className="space-y-2 max-h-48 overflow-y-auto">
              {activeClients.map((client: any) => (
                <div key={client.id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div>
                    <div className="font-medium text-gray-900 dark:text-gray-100">{client.id}</div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      User: {client.user_id}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      Connected: {formatDate(client.connected_at)}
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      Last: {formatDate(client.last_activity)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-gray-500 dark:text-gray-400 text-center py-4">
              No active clients
            </p>
          )}
        </div>

        {/* Debug Metrics */}
        {metricsData?.debug_tracker && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
              Debug Metrics
            </h3>
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Total Files</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {metricsData.debug_tracker.total_files}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Processed</div>
                <div className="text-2xl font-bold text-green-600">
                  {metricsData.debug_tracker.processed_files}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Failed</div>
                <div className="text-2xl font-bold text-red-600">
                  {metricsData.debug_tracker.failed_files}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Raw Data (Debug) */}
      {readinessData && (
        <div className="mt-6 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <details>
            <summary className="cursor-pointer text-lg font-semibold text-gray-900 dark:text-gray-100 hover:text-blue-600">
              View Raw Readiness Data
            </summary>
            <pre className="mt-4 p-4 bg-gray-100 dark:bg-gray-700 rounded-md text-sm overflow-x-auto">
              {JSON.stringify(readinessData, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  )
}
