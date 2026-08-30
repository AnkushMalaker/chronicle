import { ArrowRight, CalendarCheck2, ChevronRight, ListTodo } from 'lucide-react'
import { Link } from 'react-router-dom'
import { TimelineReviewQueueItem } from '../../services/api'
import { Button } from '../ui'
import { buildReviewCursor, ReviewCursorAction } from './reviewCursor'

function shortDate(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString([], {
    month: 'short',
    day: 'numeric',
  })
}

function ActionLine({ action, next = false }: { action: ReviewCursorAction; next?: boolean }) {
  return (
    <Link
      to={action.href}
      className={`flex items-center gap-3 rounded-md px-2.5 py-2 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)] ${next ? 'bg-[var(--tape-selected)]' : 'hover:bg-[var(--tape-chip)]'}`}
    >
      <span className="w-12 shrink-0 text-xs font-semibold text-gray-500 dark:text-gray-400">{shortDate(action.item.date)}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-semibold text-gray-900 dark:text-gray-100">{action.label}</span>
        <span className="block truncate text-[11px] text-gray-500 dark:text-gray-400">
          {action.item.episode_count} episode{action.item.episode_count === 1 ? '' : 's'}{action.attentionLabel ? ` · ${action.attentionLabel}` : ''}
        </span>
      </span>
      <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" aria-hidden="true" />
    </Link>
  )
}

export function ReviewBacklogMenu({ items, day }: { items: TimelineReviewQueueItem[]; day: string }) {
  const cursor = buildReviewCursor(items)
  if (!cursor.unresolvedCount || !cursor.next) return null
  const visible = cursor.actions.slice(0, 5)

  return (
    <details key={day} className="relative">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 rounded-md px-2 py-1 font-semibold text-[var(--tape-focus)] outline-none hover:bg-[var(--tape-chip)] focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)]">
        <ListTodo className="h-3.5 w-3.5" aria-hidden="true" />
        Episode backlog · {cursor.unresolvedCount}
      </summary>
      <div className="absolute left-0 right-auto z-40 mt-1 w-[min(20rem,calc(100vw-4rem))] rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-2 shadow-lg sm:left-auto sm:right-0 sm:w-[22rem]">
        <div className="px-2.5 pb-2 pt-1">
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">Next Timeline review</p>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">Move through unreviewed episode days in chronological order.</p>
        </div>
        <div className="space-y-0.5">
          {visible.map(action => <ActionLine key={action.item.date} action={action} next={action.item.date === cursor.next?.item.date} />)}
        </div>
        {cursor.unresolvedCount > visible.length && (
          <p className="px-2.5 py-2 text-xs text-gray-500 dark:text-gray-400">{cursor.unresolvedCount - visible.length} more days in the review trail.</p>
        )}
        <p className="mt-1 border-t border-[var(--tape-line)] px-2.5 pt-2 text-xs text-gray-500 dark:text-gray-400">Continue one Timeline day at a time.</p>
      </div>
    </details>
  )
}

export function EmptyDayHandoff({
  items,
  title,
  description,
  canAnalyze,
  analyzing,
  analyzeLabel = 'Analyze this day',
  onAnalyze,
}: {
  items: TimelineReviewQueueItem[]
  title: string
  description: string
  canAnalyze: boolean
  analyzing: boolean
  analyzeLabel?: string
  onAnalyze: () => void
}) {
  const cursor = buildReviewCursor(items)
  return (
    <section className="overflow-hidden rounded-xl border border-[var(--tape-line)] bg-[var(--tape-paper)]" aria-label="Timeline next action">
      <div className="flex items-start gap-3 p-4 sm:p-5">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--tape-chip)] text-[var(--tape-focus)]">
          <CalendarCheck2 className="h-5 w-5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{description}</p>
        </div>
      </div>

      {(cursor.next || canAnalyze) && (
        <div className="flex flex-col gap-3 border-t border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-4 sm:flex-row sm:items-center sm:justify-between">
          {cursor.next ? (
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">{cursor.unresolvedCount} day{cursor.unresolvedCount === 1 ? '' : 's'} await episode review</p>
              <p className="mt-1 truncate text-sm font-semibold text-gray-900 dark:text-gray-100">Next: {shortDate(cursor.next.item.date)} · {cursor.next.label}</p>
              {cursor.next.attentionLabel && <p className="mt-0.5 truncate text-xs text-amber-800 dark:text-amber-300">{cursor.next.attentionLabel}</p>}
            </div>
          ) : <span />}
          <div className="flex shrink-0 flex-wrap gap-2">
            {cursor.next && (
              <Link
                to={cursor.next.href}
                className="inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg bg-[var(--tape-focus)] px-3.5 text-sm font-semibold text-white outline-none transition-colors hover:brightness-95 focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)] focus-visible:ring-offset-2"
              >
                Continue review <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </Link>
            )}
            {canAnalyze && (
              <Button variant="secondary" size="md" disabled={analyzing} onClick={onAnalyze}>
                {analyzing ? 'Starting analysis…' : analyzeLabel}
              </Button>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
