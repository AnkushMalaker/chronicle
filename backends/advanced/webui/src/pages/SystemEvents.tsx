import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, AlertOctagon, Info, RefreshCw, ChevronRight, ChevronDown,
  Check, ExternalLink, ShieldAlert, type LucideIcon,
} from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useSystemEvents, useSystemEventsSummary } from '../hooks/useSystemEvents'
import { systemEventsApi, type SystemEvent, type SystemEventsFilter } from '../services/api'

// ---- Severity + category styling ------------------------------------------

type Severity = SystemEvent['severity']

const SEVERITY_STYLE: Record<Severity, { chip: string; Icon: LucideIcon; text: string }> = {
  critical: { chip: 'text-red-700 bg-red-100 dark:bg-red-900/40 dark:text-red-300', Icon: AlertOctagon, text: 'text-red-600 dark:text-red-400' },
  error: { chip: 'text-red-700 bg-red-50 dark:bg-red-900/30 dark:text-red-300', Icon: AlertTriangle, text: 'text-red-600 dark:text-red-400' },
  warning: { chip: 'text-amber-700 bg-amber-50 dark:bg-amber-900/30 dark:text-amber-300', Icon: AlertTriangle, text: 'text-amber-600 dark:text-amber-400' },
  info: { chip: 'text-blue-700 bg-blue-50 dark:bg-blue-900/30 dark:text-blue-300', Icon: Info, text: 'text-blue-600 dark:text-blue-400' },
}

const CATEGORY_CHIP: Record<string, string> = {
  service: 'text-purple-700 bg-purple-50 dark:bg-purple-900/30 dark:text-purple-300',
  client: 'text-cyan-700 bg-cyan-50 dark:bg-cyan-900/30 dark:text-cyan-300',
  pipeline: 'text-indigo-700 bg-indigo-50 dark:bg-indigo-900/30 dark:text-indigo-300',
  job: 'text-orange-700 bg-orange-50 dark:bg-orange-900/30 dark:text-orange-300',
  plugin: 'text-pink-700 bg-pink-50 dark:bg-pink-900/30 dark:text-pink-300',
  config: 'text-yellow-700 bg-yellow-50 dark:bg-yellow-900/30 dark:text-yellow-300',
  api: 'text-teal-700 bg-teal-50 dark:bg-teal-900/30 dark:text-teal-300',
  log: 'text-gray-600 bg-gray-100 dark:bg-gray-700 dark:text-gray-300',
}

const SEVERITIES: Severity[] = ['critical', 'error', 'warning', 'info']
const CATEGORIES = ['service', 'client', 'pipeline', 'job', 'plugin', 'config', 'api', 'log']

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

// ---- One event row ---------------------------------------------------------

function EventRow({ event, onAck }: { event: SystemEvent; onAck: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const sev = SEVERITY_STYLE[event.severity] ?? SEVERITY_STYLE.info
  const SevIcon = sev.Icon
  const expandable = !!(event.detail || event.traceback || Object.keys(event.metadata || {}).length)

  return (
    <div className={`rounded-lg border overflow-hidden ${event.acked ? 'border-gray-200 opacity-60 dark:border-gray-700' : 'border-gray-200 dark:border-gray-700'}`}>
      <button
        type="button"
        onClick={() => expandable && setOpen(o => !o)}
        className={`flex w-full items-center gap-3 px-3 py-2.5 text-left ${
          expandable ? 'hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer' : 'cursor-default'
        }`}
      >
        <span className="flex-shrink-0 w-4 text-gray-400">
          {expandable ? (open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />) : null}
        </span>

        <span className={`inline-flex items-center gap-1 flex-shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${sev.chip}`}>
          <SevIcon className="h-3.5 w-3.5" />
          {event.severity}
        </span>

        <span className={`flex-shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${CATEGORY_CHIP[event.category] || CATEGORY_CHIP.log}`}>
          {event.category}
        </span>

        <span className="min-w-0 flex-1 truncate text-sm text-gray-800 dark:text-gray-200" title={event.title}>
          {event.title}
        </span>

        {event.count > 1 && (
          <span className="flex-shrink-0 rounded-full bg-gray-200 px-1.5 py-0.5 text-xs font-semibold text-gray-700 dark:bg-gray-600 dark:text-gray-200" title="Times this event recurred">
            ×{event.count}
          </span>
        )}

        <span className="flex-shrink-0 hidden md:inline max-w-[12rem] truncate font-mono text-xs text-gray-400 dark:text-gray-500" title={event.source}>
          {event.source}
        </span>

        {event.client_id && (
          <span className="flex-shrink-0 hidden lg:inline font-mono text-xs text-cyan-600 dark:text-cyan-400" title="client_id">
            {event.client_id}
          </span>
        )}

        {event.conversation_id && (
          <Link
            to={`/conversations/${event.conversation_id}`}
            onClick={e => e.stopPropagation()}
            className="flex-shrink-0 hidden md:inline-flex items-center gap-0.5 text-xs text-blue-600 hover:underline dark:text-blue-400"
            title="Open related conversation"
          >
            conversation <ExternalLink className="h-3 w-3" />
          </Link>
        )}

        <span className="flex-shrink-0 w-40 text-right text-xs text-gray-500 dark:text-gray-400">
          {formatTime(event.last_seen_at || event.created_at)}
        </span>

        {!event.acked && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onAck(event.id) }}
            className="flex-shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-gray-500 hover:bg-green-50 hover:text-green-700 dark:text-gray-400 dark:hover:bg-green-900/30 dark:hover:text-green-300"
            title="Acknowledge"
          >
            <Check className="h-3.5 w-3.5" />
          </button>
        )}
      </button>

      {open && expandable && (
        <div className="border-t border-gray-100 px-4 py-3 dark:border-gray-700/60">
          {event.detail && (
            <pre className="mb-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
              {event.detail}
            </pre>
          )}
          {event.traceback && (
            <pre className="mb-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-900/20 dark:text-red-300">
              {event.traceback}
            </pre>
          )}
          {Object.keys(event.metadata || {}).length > 0 && (
            <div className="flex flex-wrap gap-2 text-xs text-gray-500 dark:text-gray-400">
              {Object.entries(event.metadata).map(([k, v]) => (
                <span key={k} className="rounded bg-gray-100 px-1.5 py-0.5 font-mono dark:bg-gray-700">
                  {k}={String(v)}
                </span>
              ))}
            </div>
          )}
          <div className="mt-2 text-xs text-gray-400 dark:text-gray-500">
            First seen {formatTime(event.created_at)}
            {event.user_id ? ` · user ${event.user_id}` : ''}
          </div>
        </div>
      )}
    </div>
  )
}

// ---- Summary strip ---------------------------------------------------------

function SummaryStrip({ summary }: { summary?: { total: number; unacked: number; by_severity: Record<string, number> } }) {
  const bySev = summary?.by_severity ?? {}
  const cards: { label: string; value: number; cls: string }[] = [
    { label: 'Total (window)', value: summary?.total ?? 0, cls: 'text-gray-900 dark:text-gray-100' },
    { label: 'Critical', value: bySev.critical ?? 0, cls: 'text-red-600 dark:text-red-400' },
    { label: 'Error', value: bySev.error ?? 0, cls: 'text-red-500 dark:text-red-400' },
    { label: 'Warning', value: bySev.warning ?? 0, cls: 'text-amber-600 dark:text-amber-400' },
    { label: 'Info', value: bySev.info ?? 0, cls: 'text-blue-600 dark:text-blue-400' },
    { label: 'Unacknowledged', value: summary?.unacked ?? 0, cls: 'text-orange-600 dark:text-orange-400' },
  ]
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {cards.map(c => (
        <div key={c.label} className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
          <div className={`text-2xl font-bold ${c.cls}`}>{c.value}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400">{c.label}</div>
        </div>
      ))}
    </div>
  )
}

// ---- Page ------------------------------------------------------------------

const WINDOWS = [
  { value: 1, label: 'Last 1h' },
  { value: 6, label: 'Last 6h' },
  { value: 24, label: 'Last 24h' },
  { value: 24 * 7, label: 'Last 7d' },
  { value: 24 * 30, label: 'Last 30d' },
]

export default function SystemEvents() {
  const { isAdmin } = useAuth()
  const queryClient = useQueryClient()

  const [severity, setSeverity] = useState('')
  const [category, setCategory] = useState('')
  const [source, setSource] = useState('')
  const [clientId, setClientId] = useState('')
  const [showAcked, setShowAcked] = useState(false)
  const [windowHours, setWindowHours] = useState(24)
  const [limit, setLimit] = useState(200)

  const filter: SystemEventsFilter = {
    ...(severity ? { severity } : {}),
    ...(category ? { category } : {}),
    ...(source ? { source } : {}),
    ...(clientId.trim() ? { client_id: clientId.trim() } : {}),
    ...(showAcked ? {} : { acked: false }),
    since_hours: windowHours,
    limit,
  }

  const { data, isLoading, error, refetch, isFetching } = useSystemEvents(filter, isAdmin)
  const { data: summary } = useSystemEventsSummary(windowHours, isAdmin)

  if (!isAdmin) {
    return (
      <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center dark:border-gray-700">
        <ShieldAlert className="mx-auto mb-3 h-8 w-8 text-gray-400" />
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Access Restricted</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400">System errors are visible to administrators only.</p>
      </div>
    )
  }

  const events = data?.events ?? []
  const sources = Object.keys(summary?.by_source ?? {})

  const onAck = (id: string) => {
    systemEventsApi.ack(id).then(() => {
      queryClient.invalidateQueries({ queryKey: ['system-events'] })
      queryClient.invalidateQueries({ queryKey: ['system-events-summary'] })
    })
  }

  const onClearAcked = () => {
    if (!window.confirm('Delete all acknowledged events?')) return
    systemEventsApi.clear(true).then(() => {
      queryClient.invalidateQueries({ queryKey: ['system-events'] })
      queryClient.invalidateQueries({ queryKey: ['system-events-summary'] })
    })
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <AlertTriangle className="h-6 w-6 text-red-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">System Errors</h1>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onClearAcked}
            className="flex items-center gap-2 rounded-md bg-gray-100 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
          >
            Clear acknowledged
          </button>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="flex items-center gap-2 rounded-md bg-gray-100 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
          >
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      <p className="mb-5 text-sm text-gray-500 dark:text-gray-400">
        Operational and application failures across the system — captured backend errors, client
        error-disconnects, failed jobs, plugin failures, and service crash-loop / recovery transitions.
        New events stream in live. Retained for 30 days.
      </p>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-300">
          {(error as Error).message || 'Failed to load system events.'}
        </div>
      )}

      <div className="mb-5">
        <SummaryStrip summary={summary} />
      </div>

      {/* Controls */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select value={severity} onChange={e => setSeverity(e.target.value)} className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          <option value="">All severities</option>
          {SEVERITIES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>

        <select value={category} onChange={e => setCategory(e.target.value)} className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          <option value="">All categories</option>
          {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>

        <select value={source} onChange={e => setSource(e.target.value)} className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          <option value="">All sources</option>
          {sources.map(s => <option key={s} value={s}>{s}</option>)}
        </select>

        <input
          type="text"
          value={clientId}
          onChange={e => setClientId(e.target.value)}
          placeholder="client_id…"
          className="w-40 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        />

        <select value={windowHours} onChange={e => setWindowHours(Number(e.target.value))} className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          {WINDOWS.map(w => <option key={w.value} value={w.value}>{w.label}</option>)}
        </select>

        <select value={limit} onChange={e => setLimit(Number(e.target.value))} className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
          {[50, 100, 200, 500].map(n => <option key={n} value={n}>Last {n}</option>)}
        </select>

        <label className="flex items-center gap-1.5 text-sm text-gray-600 dark:text-gray-300">
          <input type="checkbox" checked={showAcked} onChange={e => setShowAcked(e.target.checked)} className="rounded" />
          Show acknowledged
        </label>
      </div>

      {/* List */}
      {isLoading ? (
        <div className="flex items-center justify-center h-40">
          <div className="h-8 w-8 animate-spin rounded-full border-b-2 border-blue-600" />
        </div>
      ) : events.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-300 p-10 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
          No events for the current filters. 🎉
        </div>
      ) : (
        <div className="space-y-2">
          {events.map(ev => <EventRow key={ev.id} event={ev} onAck={onAck} />)}
          {data && data.total > events.length && (
            <div className="pt-2 text-center text-xs text-gray-400 dark:text-gray-500">
              Showing {events.length} of {data.total}. Narrow filters or raise the limit to see more.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
