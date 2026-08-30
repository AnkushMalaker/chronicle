import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, ChevronRight, FileDiff, Loader2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { TimelineDayReview, TimelineReviewQueueItem, timelineApi } from '../../services/api'
import { Button, Card, computeWordDiff, WordDiff } from '../ui'

function shortDate(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function stateLabel(item: Pick<TimelineReviewQueueItem, 'state' | 'outcome'>) {
  if (item.state === 'episodes_pending') return 'Episodes'
  if (item.state === 'memory_queued') return 'Waiting'
  if (item.state === 'memory_generating') return 'Extracting'
  if (item.state === 'memory_pending') return 'Memory'
  if (item.state === 'memory_applying') return 'Applying'
  if (item.state === 'failed') return 'Needs attention'
  if (item.outcome === 'applied') return 'Applied'
  if (item.outcome === 'rejected') return 'Rejected'
  return 'No changes'
}

function queueTone(item: TimelineReviewQueueItem, active: boolean) {
  if (active) return 'border-blue-500 bg-blue-50 text-blue-900 dark:border-blue-500 dark:bg-blue-950/30 dark:text-blue-100'
  if (item.state === 'failed' || item.unexplained_count) return 'border-amber-300 bg-white text-gray-800 dark:border-amber-800 dark:bg-gray-900 dark:text-gray-200'
  if (item.state === 'finalized') return 'border-gray-200 bg-gray-50 text-gray-500 dark:border-gray-700 dark:bg-gray-800/50 dark:text-gray-400'
  return 'border-gray-300 bg-white text-gray-800 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200'
}

function CandidateChanges({ review, day, timezone }: { review: TimelineDayReview; day: string; timezone: string }) {
  const queryClient = useQueryClient()
  const proposal = review.proposal
  const [selected, setSelected] = useState<Set<string>>(new Set())

  useEffect(() => setSelected(new Set()), [proposal?.proposal_id])

  const resolve = useMutation({
    mutationFn: (accepted: string[]) => timelineApi.resolveMemoryProposal(proposal!.proposal_id, accepted),
    onSuccess: async () => {
      setSelected(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
      ])
    },
  })
  const regenerate = useMutation({
    mutationFn: () => timelineApi.regenerateMemoryProposal(proposal!.proposal_id),
    onSuccess: async () => {
      setSelected(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
      ])
    },
  })

  if (!proposal || proposal.state !== 'pending' || !proposal.changes?.length) return null
  const changes = proposal.changes
  const allSelected = selected.size === changes.length
  const error = resolve.error as { response?: { data?: { detail?: string } }; message?: string } | null

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Potential memory changes</h4>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            These were generated from the accepted vault. Later days have not seen them.
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setSelected(allSelected ? new Set() : new Set(changes.map(change => change.change_id)))}
        >
          {allSelected ? 'Clear selection' : 'Select all'}
        </Button>
      </div>

      {changes.map(change => {
        const checked = selected.has(change.change_id)
        const diff = computeWordDiff(change.before_text ?? '', change.after_text ?? '')
        return (
          <article key={change.change_id} className={`rounded-lg border ${checked ? 'border-blue-300 bg-blue-50/50 dark:border-blue-800 dark:bg-blue-950/20' : 'border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900'}`}>
            <label className="flex cursor-pointer items-start gap-3 p-3">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => setSelected(current => {
                  const next = new Set(current)
                  next.has(change.change_id) ? next.delete(change.change_id) : next.add(change.change_id)
                  return next
                })}
                className="mt-1 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <FileDiff className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-mono text-xs font-semibold text-gray-800 dark:text-gray-200">{change.note_path}</span>
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] uppercase tracking-wide text-gray-500 dark:bg-gray-800 dark:text-gray-400">{change.operation}</span>
                </span>
                <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">{change.summary}</span>
                {!change.note_path.startsWith('Daily/') && !!change.source_episode_keys.length && (
                  <span className="mt-2 flex flex-wrap gap-1.5">
                    {change.source_episode_keys.map((episodeKey, index) => (
                      <Link
                        key={episodeKey}
                        to={`/timeline/key/${encodeURIComponent(episodeKey)}`}
                        onClick={event => event.stopPropagation()}
                        className="rounded-full bg-blue-100 px-2 py-0.5 text-[11px] font-medium text-blue-700 hover:underline dark:bg-blue-950 dark:text-blue-300"
                        title={`Open source episode ${episodeKey}`}
                      >
                        Source episode{change.source_episode_keys.length > 1 ? ` ${index + 1}` : ''}
                      </Link>
                    ))}
                  </span>
                )}
              </span>
            </label>
            <details className="border-t border-gray-100 px-3 py-2 dark:border-gray-800">
              <summary className="cursor-pointer text-xs font-medium text-gray-600 dark:text-gray-300">Inspect highlighted changes</summary>
              <div className="mt-2 grid gap-2 lg:grid-cols-2">
                <div>
                  <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                    <span>Before</span>
                    {change.before_text != null && <span className="normal-case tracking-normal text-red-600 dark:text-red-400">Removed</span>}
                  </div>
                  <pre
                    aria-label="Before text; removed words are highlighted and struck through"
                    className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-3 text-xs leading-5 text-gray-700 dark:bg-gray-950 dark:text-gray-300"
                  >
                    {change.before_text == null
                      ? <span className="italic text-gray-400">New note — no previous content</span>
                      : <WordDiff tokens={diff.beforeTokens} />}
                  </pre>
                </div>
                <div>
                  <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                    <span>Proposed</span>
                    {change.after_text != null && <span className="normal-case tracking-normal text-green-700 dark:text-green-400">Added</span>}
                  </div>
                  <pre
                    aria-label="Proposed text; added words are highlighted"
                    className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 p-3 text-xs leading-5 text-gray-700 dark:bg-gray-950 dark:text-gray-300"
                  >
                    {change.after_text == null
                      ? <span className="italic text-gray-400">Delete note — no proposed content</span>
                      : <WordDiff tokens={diff.afterTokens} />}
                  </pre>
                </div>
              </div>
            </details>
          </article>
        )
      })}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          size="sm"
          disabled={!selected.size || resolve.isPending}
          onClick={() => resolve.mutate([...selected])}
          icon={resolve.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
        >
          Apply {selected.size || ''} selected and finish day
        </Button>
        <Button variant="secondary" size="sm" disabled={resolve.isPending} onClick={() => resolve.mutate([])}>
          Reject all and finish day
        </Button>
        <Button variant="secondary" size="sm" disabled={resolve.isPending || regenerate.isPending} onClick={() => regenerate.mutate()}>
          {regenerate.isPending ? 'Regenerating…' : 'Regenerate from current vault'}
        </Button>
        {selected.size > 0 && selected.size < changes.length && (
          <span className="text-xs text-gray-500 dark:text-gray-400">The other {changes.length - selected.size} changes will be rejected.</span>
        )}
      </div>
      {resolve.isError && (
        <p className="text-xs text-red-600 dark:text-red-400">{error?.response?.data?.detail || error?.message || 'Could not resolve this proposal.'} Regenerate the proposal to compare against the current vault.</p>
      )}
      {regenerate.isError && <p className="text-xs text-red-600 dark:text-red-400">Could not regenerate this proposal. {(regenerate.error as Error)?.message}</p>}
    </div>
  )
}

export default function ReviewDesk({
  day,
  timezone,
  review,
  unexplainedCount,
  captureGapCount,
  unreconciledCount,
  onSelectDay,
}: {
  day: string
  timezone: string
  review: TimelineDayReview | null | undefined
  unexplainedCount: number
  captureGapCount: number
  unreconciledCount: number
  onSelectDay: (day: string) => void
}) {
  const queryClient = useQueryClient()
  const queue = useQuery({
    queryKey: ['timeline-review-queue', timezone],
    queryFn: async () => (await timelineApi.getReviewQueue(timezone)).data.items,
    refetchInterval: query => query.state.data?.some(item => ['memory_queued', 'memory_generating'].includes(item.state)) ? 5_000 : false,
  })
  const finalize = useMutation({
    mutationFn: () => timelineApi.finalizeEpisodes(day, timezone),
    onSuccess: async () => Promise.all([
      queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
      queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
    ]),
  })

  const visibleQueue = useMemo(() => {
    const items = queue.data || []
    const unresolved = items.filter(item => item.state !== 'finalized')
    return unresolved.length ? unresolved : items.slice(-5)
  }, [queue.data])
  const firstUnresolved = (queue.data || []).find(item => item.state !== 'finalized')
  const waitingOnEarlier = review?.state === 'memory_queued' && firstUnresolved && firstUnresolved.date !== day

  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-gray-200 px-4 py-3 dark:border-gray-700">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="font-semibold text-gray-900 dark:text-gray-100">Review ledger</h2>
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">Episodes can be prepared ahead. Memory advances one decided day at a time.</p>
          </div>
          {!!visibleQueue.length && <span className="text-xs text-gray-500 dark:text-gray-400">{visibleQueue.filter(item => item.state !== 'finalized').length} days awaiting review</span>}
        </div>

        {!!visibleQueue.length && (
          <div className="mt-3 flex items-stretch gap-1 overflow-x-auto pb-1">
            {visibleQueue.map((item, index) => (
              <div key={item.date} className="flex shrink-0 items-center">
                <button
                  type="button"
                  onClick={() => onSelectDay(item.date)}
                  className={`min-w-28 rounded-md border px-3 py-2 text-left transition-colors ${queueTone(item, item.date === day)}`}
                >
                  <span className="block text-xs font-semibold">{shortDate(item.date)}</span>
                  <span className="mt-0.5 block text-[11px]">{stateLabel(item)} · {item.episode_count} episodes</span>
                  {!!item.unexplained_count && <span className="mt-1 block text-[11px] text-amber-700 dark:text-amber-400">{item.unexplained_count} unexplained</span>}
                </button>
                {index < visibleQueue.length - 1 && <ChevronRight className="mx-0.5 h-4 w-4 text-gray-300 dark:text-gray-700" aria-hidden="true" />}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="p-4">
        {!review && <p className="text-sm text-gray-500 dark:text-gray-400">Analyze this day to begin review.</p>}

        {review?.state === 'episodes_pending' && (
          <div>
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Finish the episode account on Timeline</h3>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
              Episode correction, grouping, and the explicit handoff to potential-memory extraction now happen together on the day’s Timeline. Return here only when a memory proposal is ready to decide.
            </p>
            {(unexplainedCount > 0 || captureGapCount > 0 || unreconciledCount > 0) && (
              <div className="mt-3 flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{unexplainedCount} captured interval{unexplainedCount === 1 ? '' : 's'} remain unexplained; {captureGapCount} interval{captureGapCount === 1 ? '' : 's'} contain no capture; {unreconciledCount} newly changed range{unreconciledCount === 1 ? '' : 's'} still await reconciliation.</span>
              </div>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              <Link to={`/timeline?date=${day}`} className="inline-flex min-h-8 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2">
                Open Timeline review <ChevronRight className="h-4 w-4" />
              </Link>
            </div>
          </div>
        )}

        {review?.state === 'memory_queued' && (
          <div className="flex items-start gap-3">
            {waitingOnEarlier ? <ChevronRight className="mt-0.5 h-5 w-5 text-gray-400" /> : <Loader2 className="mt-0.5 h-5 w-5 animate-spin text-blue-600" />}
            <div>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">2. Potential memory queued</h3>
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
                {waitingOnEarlier
                  ? `${shortDate(firstUnresolved!.date)} must be decided first. This day will then run against the resulting accepted vault.`
                  : 'This day is next. Extraction will use a temporary copy of the accepted vault.'}
              </p>
            </div>
          </div>
        )}

        {review?.state === 'memory_generating' && (
          <div className="flex items-start gap-3">
            <Loader2 className="mt-0.5 h-5 w-5 animate-spin text-blue-600" />
            <div>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">2. Extracting potential memory</h3>
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">This runs against a temporary copy of the accepted vault. The next day remains blocked.</p>
            </div>
          </div>
        )}

        {review?.state === 'memory_applying' && (
          <div className="flex items-start gap-3">
            <Loader2 className="mt-0.5 h-5 w-5 animate-spin text-blue-600" />
            <div>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Applying selected memory</h3>
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">The proposal is being fenced against the accepted vault before any note changes land.</p>
            </div>
          </div>
        )}

        {review?.state === 'memory_pending' && (
          <div>
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">2. Decide what reaches the vault</h3>
            <CandidateChanges review={review} day={day} timezone={timezone} />
          </div>
        )}

        {review?.state === 'failed' && (
          <div>
            <h3 className="text-sm font-semibold text-red-700 dark:text-red-300">Potential memory extraction needs attention</h3>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">{review.error || 'The isolated extraction did not complete.'}</p>
            <Button className="mt-3" size="sm" onClick={() => finalize.mutate()} disabled={finalize.isPending}>Retry extraction</Button>
          </div>
        )}

        {review?.state === 'finalized' && (
          <div className="flex items-start gap-3">
            <Check className="mt-0.5 h-5 w-5 text-green-600 dark:text-green-400" />
            <div>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Day finalized</h3>
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
                {review.outcome === 'applied' ? 'Selected memory changes were applied.' : review.outcome === 'rejected' ? 'All potential memory was rejected.' : 'The vault already contained everything worth retaining.'} The next day may now run.
              </p>
            </div>
          </div>
        )}

        {finalize.isError && <p className="mt-3 text-xs text-red-600 dark:text-red-400">Could not advance this day. {(finalize.error as Error)?.message}</p>}
      </div>
    </Card>
  )
}
