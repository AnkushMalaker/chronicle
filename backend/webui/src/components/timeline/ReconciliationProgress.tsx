import { useEffect, useState } from 'react'
import { Check, ChevronDown, Loader2 } from 'lucide-react'

export interface JobProgress {
  stage: string
  message: string
  started_at: string
  updated_at: string
  ended_at?: string | null
  job_status?: string
  heartbeat_at?: string | null
  stages: Array<{ id: string; label: string; state: string; completed?: number; total?: number | null; unit?: string; attempt?: number }>
  events: Array<{ at: string; stage: string; message: string; state: string; attempt: number }>
}

function elapsed(start: string, end: number) {
  const seconds = Math.max(0, Math.floor((end - Date.parse(start)) / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

export default function ReconciliationProgress({ progress, status, compact = false }: {
  progress: JobProgress; status?: string; compact?: boolean
}) {
  const [now, setNow] = useState(Date.now())
  const waiting = ['queued', 'scheduled', 'deferred'].includes(status || progress.job_status || '')
  const terminal = ['completed', 'finished', 'failed', 'stopped', 'canceled'].includes(status || progress.job_status || '')
  const awaitingRecovery = waiting && ['failed', 'stopped', 'canceled'].includes(progress.job_status || '')
  const failed = ['failed', 'stopped', 'canceled'].includes(status || progress.job_status || '') && terminal
  const message = failed ? 'Reconciliation failed' : awaitingRecovery ? 'Attempt failed; awaiting recovery scheduler' : progress.message
  useEffect(() => {
    if (terminal) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [terminal])
  const current = progress.stages.find(stage => stage.id === progress.stage)
  const known = current?.total != null && current.total > 0
  const done = current?.completed || 0
  const end = terminal ? Math.max(Date.parse(progress.ended_at || progress.updated_at), Date.parse(progress.updated_at)) : now
  return (
    <section aria-label="Reconciliation progress" className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-4 text-gray-800 dark:text-gray-200">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-500 dark:text-gray-400">Reconciliation · {terminal ? status || progress.job_status : awaitingRecovery ? 'Awaiting recovery' : waiting ? 'Queued' : 'In progress'}</p>
          <h3 className="mt-1 flex items-center gap-2 text-sm font-semibold">
            {!terminal && !waiting && <Loader2 aria-hidden className="h-4 w-4 animate-spin text-[var(--tape-media)]" />}
            {message}
          </h3>
        </div>
        <span className="font-mono text-xs tabular-nums text-gray-500 dark:text-gray-400">{elapsed(progress.started_at, end)} elapsed</span>
      </div>
      <ol className={`mt-4 grid gap-1.5 ${compact ? 'grid-cols-3' : 'grid-cols-3 sm:grid-cols-6'}`}>
        {progress.stages.map((stage, index) => (
          <li key={stage.id} aria-current={stage.id === progress.stage ? 'step' : undefined}>
            <div className={`h-1.5 rounded-full ${stage.state === 'completed' ? 'bg-[var(--tape-media)]' : stage.id === progress.stage ? 'bg-[var(--tape-media)] opacity-50' : 'bg-[var(--tape-line)]'}`} />
            <span className={`mt-1.5 flex items-center gap-1 text-[11px] ${stage.id === progress.stage ? 'font-semibold' : 'text-gray-500 dark:text-gray-400'}`}>
              {stage.state === 'completed' ? <Check aria-label="Complete" className="h-3 w-3 shrink-0" /> : <span className="font-mono">{index + 1}</span>}
              {stage.label}
            </span>
          </li>
        ))}
      </ol>
      {!terminal && !waiting && current && <div className="mt-4">
        <div className="flex justify-between gap-2 text-xs">
          <span>{current.label}{(current.attempt || 1) > 1 && ` · attempt ${current.attempt}`}</span>
          <span className="font-mono tabular-nums">{known ? `${done}/${current.total} ${current.unit}` : 'Working…'}</span>
        </div>
        {known && <div role="progressbar" aria-label={current.label} aria-valuemin={0} aria-valuemax={current.total!} aria-valuenow={done} className="mt-2 h-2 overflow-hidden rounded-full bg-[var(--tape-line)]">
          <div className="h-full rounded-full bg-[var(--tape-media)] transition-[width]" style={{ width: `${Math.min(100, 100 * done / current.total!)}%` }} />
        </div>}
        <p className="mt-2 text-[11px] text-gray-500 dark:text-gray-400">Last activity {elapsed(progress.updated_at, now)} ago. Counts track work, not time remaining.</p>
        {progress.heartbeat_at && <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">Worker heartbeat {elapsed(progress.heartbeat_at, now)} ago</p>}
      </div>}
      <details className="mt-3 border-t border-[var(--tape-line)] pt-2">
        <summary className="flex cursor-pointer items-center gap-1 text-xs text-gray-500 dark:text-gray-400"><ChevronDown className="h-3 w-3" />Activity · {progress.events.length} {progress.events.length === 1 ? 'update' : 'updates'}</summary>
        <ol className="mt-2 max-h-44 space-y-2 overflow-auto text-xs">
          {[...progress.events].reverse().map((event, i) => <li key={i} className="flex gap-3">
            <span className="shrink-0 font-mono text-gray-500 dark:text-gray-400">+{elapsed(progress.started_at, Date.parse(event.at))}</span>
            <span>{event.message}</span>
          </li>)}
        </ol>
      </details>
    </section>
  )
}
