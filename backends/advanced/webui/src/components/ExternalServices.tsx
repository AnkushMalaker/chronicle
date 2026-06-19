import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CheckCircle, Circle, Play, RefreshCw, RotateCcw, Square, Wrench, XCircle } from 'lucide-react'
import { systemApi } from '../services/api'
import { useExternalServices, ExternalService, ServiceOperation } from '../hooks/useSystem'

const ACTION_LABELS: Record<string, string> = {
  start: 'Starting',
  stop: 'Stopping',
  restart: 'Restarting',
}

function operationLabel(op: ServiceOperation): string {
  if (op.action.startsWith('provider:')) {
    return `Switching ${op.service} to ${op.action.split(':')[1]}`
  }
  return `${ACTION_LABELS[op.action] ?? op.action} ${op.service}`
}

function HealthBadge({ service }: { service: ExternalService }) {
  switch (service.health) {
    case 'healthy':
      return <CheckCircle className="h-5 w-5 text-green-500 flex-shrink-0" />
    case 'starting':
      return <RefreshCw className="h-5 w-5 text-blue-500 animate-spin flex-shrink-0" />
    case 'partial':
      return <AlertCircle className="h-5 w-5 text-yellow-500 flex-shrink-0" />
    case 'unhealthy':
      return <XCircle className="h-5 w-5 text-red-500 flex-shrink-0" />
    default:
      return <Circle className="h-5 w-5 text-gray-400 flex-shrink-0" />
  }
}

export default function ExternalServices({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient()
  const [activeOp, setActiveOp] = useState<ServiceOperation | null>(null)
  const [lastFailedOp, setLastFailedOp] = useState<ServiceOperation | null>(null)
  const [buildImages, setBuildImages] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const busy = activeOp?.status === 'running'
  const { data, isLoading } = useExternalServices(isAdmin, busy)

  // Adopt an operation already running on the agent (e.g. started in another tab)
  useEffect(() => {
    if (!activeOp && data?.operation && data.operation.status === 'running') {
      setActiveOp(data.operation)
    }
  }, [data?.operation, activeOp])

  // Poll the active operation until it finishes
  useEffect(() => {
    if (!activeOp || activeOp.status !== 'running') return
    const timer = setInterval(async () => {
      try {
        const response = await systemApi.getExternalServiceOperation(activeOp.id)
        const op: ServiceOperation = response.data
        if (op.status !== 'running') {
          setActiveOp(null)
          if (op.status === 'failed') setLastFailedOp(op)
          queryClient.invalidateQueries({ queryKey: ['system'] })
        } else {
          setActiveOp(op)
        }
      } catch {
        // Backend briefly unreachable (e.g. backend restart) — keep polling
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [activeOp, queryClient])

  const runAction = async (service: ExternalService, action: 'start' | 'stop' | 'restart') => {
    setError(null)
    setLastFailedOp(null)
    if (service.name === 'backend' && action !== 'start') {
      const confirmed = window.confirm(
        'Stopping the backend takes down this dashboard — you will need ./start.sh on the host to bring it back. Continue?'
      )
      if (!confirmed) return
    }
    try {
      const response = await systemApi.externalServiceAction(service.name, action, {
        build: buildImages,
        force: service.name === 'backend',
      })
      setActiveOp(response.data.operation)
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    }
  }

  const switchProvider = async (
    service: ExternalService,
    provider: string,
    lane: 'batch' | 'streaming' = 'batch',
  ) => {
    const current = lane === 'streaming' ? service.provider?.streaming_current : service.provider?.current
    if (!provider || provider === current) return
    setError(null)
    setLastFailedOp(null)
    try {
      const response = await systemApi.setExternalServiceProvider(service.name, provider, buildImages, lane)
      setActiveOp(response.data.operation)
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    }
  }

  if (!isAdmin || isLoading) return null

  if (!data?.available) {
    if (data?.reason === 'unreachable') {
      return (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2 flex items-center">
            <Wrench className="h-5 w-5 mr-2 text-blue-600" />
            External Services
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Service manager agent is not reachable. Start it on the host with{' '}
            <code className="px-1 bg-gray-100 dark:bg-gray-700 rounded">./start.sh</code> or{' '}
            <code className="px-1 bg-gray-100 dark:bg-gray-700 rounded">uv run python services.py manager start</code>.
          </p>
        </div>
      )
    }
    // Not configured at all — hide the section
    return null
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
          <Wrench className="h-5 w-5 mr-2 text-blue-600" />
          External Services
        </h3>
        <label className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
          <input
            type="checkbox"
            checked={buildImages}
            onChange={e => setBuildImages(e.target.checked)}
            className="rounded border-gray-300"
          />
          <span>Build images</span>
        </label>
      </div>

      {/* Active operation banner */}
      {busy && activeOp && (
        <div className="mb-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md p-3 flex items-center space-x-2">
          <RefreshCw className="h-4 w-4 text-blue-500 animate-spin" />
          <span className="text-sm text-blue-700 dark:text-blue-300">
            {operationLabel(activeOp)}
            {activeOp.phase ? ` — ${activeOp.phase}` : '… this can take a few minutes.'}
          </span>
        </div>
      )}

      {/* Failure detail */}
      {lastFailedOp && (
        <div className="mb-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-red-700 dark:text-red-300">
              {operationLabel(lastFailedOp)} failed.
            </span>
            <button onClick={() => setLastFailedOp(null)} className="text-red-500 hover:text-red-700">
              <XCircle className="h-4 w-4" />
            </button>
          </div>
          {lastFailedOp.log && (
            <details className="mt-2">
              <summary className="text-xs text-red-600 dark:text-red-400 cursor-pointer">Show log</summary>
              <pre className="mt-1 p-2 bg-red-100 dark:bg-red-900/40 rounded text-xs overflow-x-auto max-h-48 text-red-800 dark:text-red-200">
                {lastFailedOp.log}
              </pre>
            </details>
          )}
        </div>
      )}

      {error && (
        <div className="mb-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3">
          <span className="text-sm text-red-700 dark:text-red-300">{error}</span>
        </div>
      )}

      <div className="space-y-3">
        {(data.services ?? []).filter(s => s.enabled).map(service => {
          const stopped = service.health === 'stopped'
          const starting = service.health === 'starting'
          return (
            <div key={service.name} className="p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center space-x-3 min-w-0">
                  <HealthBadge service={service} />
                  <div className="min-w-0">
                    <div className="font-medium text-gray-900 dark:text-gray-100">
                      {service.name}
                      <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">
                        :{service.ports.join(', :')}
                      </span>
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-400 truncate">
                      {service.description}
                      {starting ? (
                        <span className="text-blue-600 dark:text-blue-400"> — starting (model loading can take minutes)</span>
                      ) : service.health_detail ? (
                        <span className="text-yellow-600 dark:text-yellow-400"> — {service.health_detail}</span>
                      ) : null}
                    </div>
                  </div>
                </div>

                <div className="flex items-center space-x-2">
                  {service.provider && service.provider.available.length > 0 && (
                    <div className="flex flex-col gap-1">
                      <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
                        {service.provider.streaming_available?.length ? <span className="w-14 shrink-0">Batch</span> : null}
                        <select
                          value={service.provider.current}
                          onChange={e => switchProvider(service, e.target.value, 'batch')}
                          disabled={busy}
                          className="text-sm px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 disabled:opacity-50"
                          title="Active batch (file/full-audio) provider — changing switches and restarts the service"
                        >
                          {!service.provider.current && <option value="">(no provider set)</option>}
                          {service.provider.available.map(p => (
                            <option key={p.key} value={p.key}>{p.label}</option>
                          ))}
                        </select>
                      </label>
                      {service.provider.streaming_available && service.provider.streaming_available.length > 0 && (
                        <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
                          <span className="w-14 shrink-0">Streaming</span>
                          <select
                            value={service.provider.streaming_current ?? ''}
                            onChange={e => switchProvider(service, e.target.value, 'streaming')}
                            disabled={busy}
                            className="text-sm px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 disabled:opacity-50"
                            title="Active streaming (live transcription) provider — changing switches and restarts the service"
                          >
                            {!service.provider.streaming_current && <option value="">(no provider set)</option>}
                            {service.provider.streaming_available.map(p => (
                              <option key={p.key} value={p.key}>{p.label}</option>
                            ))}
                          </select>
                        </label>
                      )}
                    </div>
                  )}

                  {stopped ? (
                    <button
                      onClick={() => runAction(service, 'start')}
                      disabled={busy}
                      className="flex items-center space-x-1 px-3 py-1.5 text-sm bg-green-600 text-white rounded-md hover:bg-green-700 transition-colors disabled:opacity-50"
                    >
                      <Play className="h-3.5 w-3.5" />
                      <span>Start</span>
                    </button>
                  ) : starting ? (
                    <button
                      onClick={() => runAction(service, 'stop')}
                      disabled={busy}
                      className="flex items-center space-x-1 px-3 py-1.5 text-sm bg-red-600 text-white rounded-md hover:bg-red-700 transition-colors disabled:opacity-50"
                    >
                      <Square className="h-3.5 w-3.5" />
                      <span>Stop</span>
                    </button>
                  ) : (
                    <>
                      <button
                        onClick={() => runAction(service, 'restart')}
                        disabled={busy}
                        className="flex items-center space-x-1 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        <span>Restart</span>
                      </button>
                      <button
                        onClick={() => runAction(service, 'stop')}
                        disabled={busy}
                        className="flex items-center space-x-1 px-3 py-1.5 text-sm bg-red-600 text-white rounded-md hover:bg-red-700 transition-colors disabled:opacity-50"
                      >
                        <Square className="h-3.5 w-3.5" />
                        <span>Stop</span>
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
        Managed by the host service-manager agent. Provider changes stop the old container before
        starting the new one; GPU models may take a few minutes to load after start.
      </p>
    </div>
  )
}
