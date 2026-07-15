import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AlertCircle, ArrowUpCircle, Check, CheckCircle, Circle, Play, RefreshCw, RotateCcw, Server, Square, Wrench, XCircle } from 'lucide-react'
import { systemApi } from '../services/api'
import { useExternalServices, ExternalService, ServiceOperation, UpdateCheckResult } from '../hooks/useSystem'

type Lane = 'batch' | 'streaming'

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

// Per-node code-version + update control (lifecycle mode only). Checks the node's
// git state against a target ref (default from the backend), then offers to run the
// update. The resulting operation is polled by the shared operation banner via
// `onStarted` → the parent's setActiveOp, so progress (`phase`) shows live.
function NodeUpdateControl({
  node,
  isHubNode,
  busy,
  onStarted,
}: {
  node: string | null
  isHubNode: boolean
  busy: boolean
  onStarted: (op: ServiceOperation) => void
}) {
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState<UpdateCheckResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  const check = async () => {
    setChecking(true)
    setError(null)
    try {
      const response = await systemApi.checkNodeUpdate(node)
      setResult(response.data)
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    } finally {
      setChecking(false)
    }
  }

  const startUpdate = async () => {
    const message = isHubNode
      ? 'Update the hub node?\n\nThis restarts the backend — the WebUI will briefly disconnect and the page may need a reload.'
      : `Update node ${node ?? ''}?\n\nThis restarts the node agent and backend on that host.`
    if (!window.confirm(message)) return
    setStarting(true)
    setError(null)
    try {
      const response = await systemApi.startNodeUpdate({ node: node ?? undefined })
      onStarted(response.data.operation)
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    } finally {
      setStarting(false)
    }
  }

  // Check not run yet — offer the check action.
  if (!result && !checking && !error) {
    return (
      <div className="px-1">
        <button
          onClick={check}
          disabled={busy}
          className="flex items-center gap-1.5 text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50"
        >
          <ArrowUpCircle className="h-3.5 w-3.5" />
          <span>Check for updates</span>
        </button>
      </div>
    )
  }

  if (checking) {
    return (
      <div className="flex items-center gap-1.5 px-1 text-xs text-gray-500 dark:text-gray-400">
        <RefreshCw className="h-3.5 w-3.5 text-blue-500 animate-spin" />
        <span>Checking for updates… (this can take up to a minute)</span>
      </div>
    )
  }

  // Network / request failure on the check itself.
  if (error && !result) {
    return (
      <div className="flex items-center gap-2 px-1 text-xs">
        <span className="text-red-600 dark:text-red-400">Update check failed: {error}</span>
        <button onClick={check} className="text-blue-600 dark:text-blue-400 hover:underline">
          Retry
        </button>
      </div>
    )
  }

  if (!result) return null

  // Update mechanism not available on this node (e.g. not a git checkout).
  if (!result.available) {
    return (
      <div className="px-1 text-xs text-gray-400 dark:text-gray-500">
        Updates unavailable{result.reason ? `: ${result.reason}` : ''}
        {result.detail ? ` (${result.detail})` : ''}
      </div>
    )
  }

  const current = result.current
  const target = result.target
  const updateAvailable = Boolean(result.update_available && target)

  return (
    <div className="flex flex-wrap items-center gap-2 px-1 text-xs">
      {updateAvailable && target ? (
        <>
          <span className="text-amber-600 dark:text-amber-400 font-medium">Update available:</span>
          <span className="font-mono text-gray-600 dark:text-gray-300">
            {current?.describe ?? '?'} → {target.ref} ({target.commit.slice(0, 7)})
          </span>
          <button
            onClick={startUpdate}
            disabled={busy || starting}
            className="flex items-center gap-1 px-2 py-1 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            <ArrowUpCircle className="h-3.5 w-3.5" />
            <span>Update node</span>
          </button>
        </>
      ) : (
        <>
          <CheckCircle className="h-3.5 w-3.5 text-green-500" />
          <span className="text-gray-500 dark:text-gray-400">
            Up to date{current?.describe ? ` (${current.describe})` : ''}
          </span>
          <button onClick={check} disabled={busy} className="text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50">
            Re-check
          </button>
        </>
      )}
      {result.error && (
        <span className="text-red-600 dark:text-red-400">— {result.error}</span>
      )}
      {error && (
        <span className="text-red-600 dark:text-red-400">— {error}</span>
      )}
    </div>
  )
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

// 'lifecycle' (System page) shows health + start/stop/restart buttons.
// 'providers' (Settings page) shows the batch/streaming ASR/TTS provider dropdowns.
// Both share the /admin/services data + operation polling; only the right-hand
// controls differ. The two are never mounted at the same time.
type ExternalServicesMode = 'lifecycle' | 'providers'

export default function ExternalServices({
  isAdmin,
  mode = 'lifecycle',
}: {
  isAdmin: boolean
  mode?: ExternalServicesMode
}) {
  const title = mode === 'providers' ? 'ASR / TTS Providers' : 'External Services'
  const queryClient = useQueryClient()
  const [activeOp, setActiveOp] = useState<ServiceOperation | null>(null)
  const [lastFailedOp, setLastFailedOp] = useState<ServiceOperation | null>(null)
  const [buildImages, setBuildImages] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Staged provider selections (keyed by node:service:lane). A change is held here
  // until the user clicks Apply — a switch can be heavy (start/stop a container,
  // sometimes a model download), so we don't fire it on every dropdown change.
  const [pending, setPending] = useState<Record<string, string>>({})

  const busy = activeOp?.status === 'running'
  const { data, isLoading, isFetching } = useExternalServices(isAdmin, busy)

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
        const response = await systemApi.getExternalServiceOperation(activeOp.id, activeOp.node)
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
        node: service.node,
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
      const response = await systemApi.setExternalServiceProvider(service.name, provider, buildImages, lane, service.node)
      setActiveOp(response.data.operation)
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    }
  }

  // ── Staged provider selection (apply on demand, not on select) ──────────────
  const provKey = (service: ExternalService, lane: Lane) =>
    `${service.node ?? 'local'}:${service.name}:${lane}`

  const laneCurrent = (service: ExternalService, lane: Lane) =>
    (lane === 'streaming' ? service.provider?.streaming_current : service.provider?.current) ?? ''

  const laneOptions = (service: ExternalService, lane: Lane) =>
    (lane === 'streaming' ? service.provider?.streaming_available : service.provider?.available) ?? []

  const stagedValue = (service: ExternalService, lane: Lane) =>
    pending[provKey(service, lane)] ?? laneCurrent(service, lane)

  const hasPending = (service: ExternalService, lane: Lane) => {
    const v = pending[provKey(service, lane)]
    return v != null && v !== laneCurrent(service, lane)
  }

  // Heavy = a local container is started/stopped (and may need a model download).
  // Only a move between two cloud providers is config-only.
  const pendingIsHeavy = (service: ExternalService, lane: Lane) => {
    const opts = laneOptions(service, lane)
    const next = opts.find(o => o.key === pending[provKey(service, lane)])
    const cur = opts.find(o => o.key === laneCurrent(service, lane))
    return Boolean(next?.local) || Boolean(cur?.local)
  }

  const stageProvider = (service: ExternalService, lane: Lane, value: string) =>
    setPending(p => ({ ...p, [provKey(service, lane)]: value }))

  const cancelPending = (service: ExternalService, lane: Lane) =>
    setPending(p => {
      const next = { ...p }
      delete next[provKey(service, lane)]
      return next
    })

  const applyProvider = async (service: ExternalService, lane: Lane) => {
    const provider = pending[provKey(service, lane)]
    if (provider == null) return
    await switchProvider(service, provider, lane)
    cancelPending(service, lane)
  }

  // Apply / discard controls shown inline once a lane has a pending change.
  const renderApply = (service: ExternalService, lane: Lane) => {
    if (!hasPending(service, lane)) return null
    const heavy = pendingIsHeavy(service, lane)
    return (
      <span className="flex items-center gap-1.5">
        <button
          onClick={() => applyProvider(service, lane)}
          disabled={busy}
          className="flex items-center gap-1 px-2 py-1 text-xs bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50"
          title={heavy
            ? 'Apply: stops the current container and starts the new provider — model load/download can take minutes'
            : 'Apply: config change only — no container restart'}
        >
          <Check className="h-3 w-3" />
          <span>Apply</span>
        </button>
        <span className={`text-[10px] whitespace-nowrap ${heavy ? 'text-amber-600 dark:text-amber-400' : 'text-green-600 dark:text-green-400'}`}>
          {heavy ? 'restarts service' : 'config only'}
        </span>
        <button
          onClick={() => cancelPending(service, lane)}
          disabled={busy}
          className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 disabled:opacity-50"
          title="Discard pending change"
        >
          <XCircle className="h-3.5 w-3.5" />
        </button>
      </span>
    )
  }

  if (!isAdmin) return null

  // Initial load — show a skeleton card so it's clear status is being fetched
  // (the node-agent / cluster discovery can take a few seconds), rather than a
  // blank space where the section will appear.
  if (isLoading) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Wrench className="h-5 w-5 mr-2 text-blue-600" />
          {title}
        </h3>
        <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
          <RefreshCw className="h-4 w-4 text-blue-500 animate-spin" />
          <span>Loading services…</span>
        </div>
      </div>
    )
  }

  if (!data?.available) {
    if (data?.reason === 'unreachable') {
      return (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2 flex items-center">
            <Wrench className="h-5 w-5 mr-2 text-blue-600" />
            {title}
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

  // 'providers' mode lists every service that exposes provider switching (ASR/TTS),
  // even when disabled — the dropdown is the only way to switch to a local provider
  // (which re-enables it), so hiding a disabled service would strand it on whatever
  // cloud provider is selected with no UI path to change it.
  // 'lifecycle' mode lists enabled services (those that have start/stop/restart).
  const visibleServices = (data.services ?? []).filter(s =>
    mode === 'providers'
      ? s.provider != null && s.provider.available.length > 0
      : s.enabled
  )
  // Group services by node. With only the local node this is a single flat
  // group (no header, unchanged look); in a cluster, each node gets its own header.
  const byNode = new Map<string, ExternalService[]>()
  for (const s of visibleServices) {
    const key = s.node ?? 'local'
    const list = byNode.get(key) ?? []
    list.push(s)
    byNode.set(key, list)
  }
  // Local node first, then remote nodes alphabetically.
  const serviceGroups = Array.from(byNode.entries()).sort(([, a], [, b]) => {
    const ar = a[0]?.remote ? 1 : 0
    const br = b[0]?.remote ? 1 : 0
    if (ar !== br) return ar - br
    return (a[0]?.node ?? '').localeCompare(b[0]?.node ?? '')
  })
  const showNodeHeaders = serviceGroups.length > 1 || visibleServices.some(s => s.remote)

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
          <Wrench className="h-5 w-5 mr-2 text-blue-600" />
          {title}
          {/* Background poll in flight (e.g. waiting for a 'starting' service to
              come up, or the periodic refetch) — subtle hint that status is live. */}
          {isFetching && (
            <span className="ml-2 flex items-center gap-1 text-xs font-normal text-gray-400 dark:text-gray-500">
              <RefreshCw className="h-3 w-3 animate-spin" />
              refreshing…
            </span>
          )}
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

      <div className="space-y-4">
        {serviceGroups.map(([nodeKey, groupServices]) => (
          <div key={nodeKey} className="space-y-2">
            {showNodeHeaders && (
              <div className="flex items-center gap-2 px-1">
                <Server className="h-4 w-4 text-gray-500 dark:text-gray-400" />
                <span className="font-mono text-sm font-medium text-gray-700 dark:text-gray-300">
                  {groupServices[0]?.node ?? nodeKey}
                </span>
                <span
                  className={`text-xs px-1.5 py-0.5 rounded ${
                    groupServices[0]?.remote
                      ? 'bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300'
                      : 'bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300'
                  }`}
                >
                  {groupServices[0]?.remote ? 'remote node' : 'hub'}
                </span>
              </div>
            )}
            {mode === 'lifecycle' && (
              <NodeUpdateControl
                node={groupServices[0]?.node ?? null}
                isHubNode={
                  groupServices.some(s => s.name === 'backend' && s.remote === false) ||
                  !groupServices[0]?.remote
                }
                busy={busy}
                onStarted={op => {
                  setError(null)
                  setLastFailedOp(null)
                  setActiveOp(op)
                }}
              />
            )}
            {groupServices.map(service => {
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
                  {mode === 'providers' && service.provider && service.provider.available.length > 0 && (
                    <div className="flex flex-col gap-1">
                      <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
                        {service.provider.streaming_available?.length ? <span className="w-14 shrink-0">Batch</span> : null}
                        <select
                          value={stagedValue(service, 'batch')}
                          onChange={e => stageProvider(service, 'batch', e.target.value)}
                          disabled={busy}
                          className="text-sm px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 disabled:opacity-50"
                          title="Active batch (file/full-audio) provider — choose, then click Apply to switch"
                        >
                          {!service.provider.current && <option value="">(no provider set)</option>}
                          {service.provider.available.map(p => (
                            <option key={p.key} value={p.key}>{p.label}</option>
                          ))}
                        </select>
                        {renderApply(service, 'batch')}
                      </label>
                      {service.provider.streaming_available && service.provider.streaming_available.length > 0 && (
                        <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
                          <span className="w-14 shrink-0">Streaming</span>
                          <select
                            value={stagedValue(service, 'streaming')}
                            onChange={e => stageProvider(service, 'streaming', e.target.value)}
                            disabled={busy}
                            className="text-sm px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 disabled:opacity-50"
                            title="Active streaming (live transcription) provider — choose, then click Apply to switch"
                          >
                            {!service.provider.streaming_current && <option value="">(no provider set)</option>}
                            {service.provider.streaming_available.map(p => (
                              <option key={p.key} value={p.key}>{p.label}</option>
                            ))}
                          </select>
                          {renderApply(service, 'streaming')}
                        </label>
                      )}
                    </div>
                  )}

                  {mode === 'providers' ? (
                    // Provider-config surface: no lifecycle buttons here. When a
                    // service is disabled in config.yml, selecting a local provider
                    // above re-enables it — guide the user there.
                    !service.enabled ? (
                      <span className="text-xs text-gray-500 dark:text-gray-400 italic">
                        not in startup set — pick a local provider to enable
                      </span>
                    ) : null
                  ) : stopped ? (
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
        ))}
      </div>

      <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
        {mode === 'providers'
          ? 'Provider changes stop the old container before starting the new one; GPU models may take a few minutes to load after start. This switches the running service and its model together — use Settings → Active Models to repoint a role at a model without a container.'
          : 'Managed by the host service-manager agent. Start/stop/restart the container stack here.'}
      </p>
    </div>
  )
}
