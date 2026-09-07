import { useState } from 'react'
import { AlertTriangle, Check, ChevronRight, Loader2, LockKeyhole, Sparkles } from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  TimelineConsolidationProposal,
  DayReviewProjection,
  TimelineDayReview,
  TimelineDirtyRange,
  TimelineEpisode,
} from '../../services/api'
import { Button } from '../ui'
import SessionStructureReview from './SessionStructureReview'

interface EpisodeReviewCheckpointProps {
  day: string
  timezone: string
  review: TimelineDayReview | null
  episodeCount: number
  eligibleCount: number
  referenceOnlyCount: number
  unreconciledRanges: TimelineDirtyRange[]
  unstableEpisodes: TimelineEpisode[]
  consolidation: TimelineConsolidationProposal | null
  finalizing: boolean
  dismissingRangeId?: string | null
  rejectingEpisodeId?: string | null
  rejectionError?: Error | null
  onNotActivity: (episode: TimelineEpisode) => void
  confirmingSessionId?: string | null
  projection: DayReviewProjection
  episodes: TimelineEpisode[]
  error?: Error | null
  dismissalError?: Error | null
  confirmationError?: Error | null
  onReviewGrouping: () => void
  onDismissRange: (dirtyRangeId: string, reason: string) => void
  onConfirmStructures: (sessionId: string, episodes: TimelineEpisode[]) => void
  onEditEpisode: (episode: TimelineEpisode) => void
  onFinish: () => void
}

function FailedRangeResolution({
  range,
  timezone,
  busy,
  onDismiss,
}: {
  range: TimelineDirtyRange
  timezone: string
  busy: boolean
  onDismiss: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const label = `${new Date(range.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZone: timezone })}–${new Date(range.ended_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZone: timezone })}`
  const reasonId = `dismiss-range-${range.dirty_range_id}`

  return (
    <div className="rounded-md border border-amber-300 bg-amber-50/70 p-3 dark:border-amber-900 dark:bg-amber-950/20">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-xs font-semibold text-amber-950 dark:text-amber-100">Failed range {label}</p>
        <span className="text-[11px] text-amber-800/80 dark:text-amber-300/80">{range.attempts} attempt{range.attempts === 1 ? '' : 's'}</span>
      </div>
      {range.error && (range.error.length > 240 ? (
        <details className="mt-1 text-xs leading-5 text-amber-900 dark:text-amber-200">
          <summary className="cursor-pointer">Reconciliation failed · Show error details</summary>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border border-amber-200 p-2 font-mono text-[11px] dark:border-amber-900">{range.error}</pre>
        </details>
      ) : <p className="mt-1 text-xs leading-5 text-amber-900 dark:text-amber-200">{range.error}</p>)}
      <label htmlFor={reasonId} className="mt-2 block text-xs font-medium text-gray-700 dark:text-gray-200">Why is it acceptable to leave this interval unresolved?</label>
      <textarea
        id={reasonId}
        value={reason}
        rows={2}
        onChange={event => setReason(event.target.value)}
        aria-label={`Reason for failed range ${label}`}
        placeholder="Record what you reviewed and why this should stop blocking the day."
        className="mt-1 w-full resize-y rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-2.5 py-2 text-xs text-gray-900 outline-none placeholder:text-gray-400 focus:border-[var(--tape-focus)] focus:ring-1 focus:ring-[var(--tape-focus)] dark:text-gray-100"
      />
      <div className="mt-2 flex justify-end">
        <Button aria-label={`Dismiss failed range ${label}`} size="sm" variant="secondary" disabled={busy || !reason.trim()} onClick={() => onDismiss(reason.trim())} icon={busy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}>
          {busy ? 'Dismissing…' : 'Dismiss failed range'}
        </Button>
      </div>
    </div>
  )
}

function CountSummary({ episodeCount, eligibleCount, referenceOnlyCount }: Pick<EpisodeReviewCheckpointProps, 'episodeCount' | 'eligibleCount' | 'referenceOnlyCount'>) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
      <span><strong className="font-semibold text-gray-700 dark:text-gray-200">{episodeCount}</strong> episode{episodeCount === 1 ? '' : 's'}</span>
      <span><strong className="font-semibold text-gray-700 dark:text-gray-200">{eligibleCount}</strong> memory-eligible</span>
      {!!referenceOnlyCount && <span><strong className="font-semibold text-gray-700 dark:text-gray-200">{referenceOnlyCount}</strong> reference-only</span>}
    </div>
  )
}

export default function EpisodeReviewCheckpoint({
  day,
  timezone,
  review,
  episodeCount,
  eligibleCount,
  referenceOnlyCount,
  unreconciledRanges,
  unstableEpisodes,
  consolidation,
  finalizing,
  dismissingRangeId,
  confirmingSessionId,
  rejectingEpisodeId,
  rejectionError,
  onNotActivity,
  projection,
  episodes,
  error,
  dismissalError,
  confirmationError,
  onReviewGrouping,
  onDismissRange,
  onConfirmStructures,
  onEditEpisode,
  onFinish,
}: EpisodeReviewCheckpointProps) {
  const pendingSuggestions = consolidation?.state === 'ready' && consolidation.suggestions.length > 0
  const groupingInProgress = consolidation?.state === 'queued' || consolidation?.state === 'generating'
  const failedRanges = unreconciledRanges.filter(range => range.state === 'failed')
  const unreconciledCount = unreconciledRanges.length

  if (failedRanges.length) {
    return (
      <section id="episode-review-checkpoint" aria-live="polite" className="rounded-lg border border-[var(--tape-focus)] bg-[var(--tape-selected)] px-3 py-3 sm:px-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" />
            <div>
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Resolve failed reconciliation</h2>
              <p className="mt-0.5 max-w-3xl text-xs leading-5 text-gray-600 dark:text-gray-300">
                {failedRanges.length} terminal range{failedRanges.length === 1 ? '' : 's'} could not be reconciled. Retry the day, or record why each unresolved interval is acceptable before finishing.
              </p>
              <CountSummary episodeCount={episodeCount} eligibleCount={eligibleCount} referenceOnlyCount={referenceOnlyCount} />
            </div>
          </div>
          <span className="shrink-0 text-xs font-medium text-amber-800 dark:text-amber-300">Dismissal reason required</span>
        </div>
        <div className="mt-3 grid gap-2 border-t border-[var(--tape-line)] pt-3">
          {failedRanges.map(range => (
            <FailedRangeResolution
              key={range.dirty_range_id}
              range={range}
              timezone={timezone}
              busy={dismissingRangeId === range.dirty_range_id}
              onDismiss={reason => onDismissRange(range.dirty_range_id, reason)}
            />
          ))}
        </div>
        {dismissalError && <p className="mt-2 text-xs text-red-700 dark:text-red-300">Could not dismiss the failed range. {dismissalError.message}</p>}
      </section>
    )
  }

  if (!review) return null

  if (review.state === 'episodes_pending') {
    const reviewingSessions = !pendingSuggestions && !groupingInProgress && !unreconciledCount && unstableEpisodes.length > 0
    const title = pendingSuggestions
      ? 'Decide the suggested grouping'
      : groupingInProgress
        ? 'Grouping review is running'
        : unreconciledCount
          ? 'Episode review is waiting for reconciliation'
          : unstableEpisodes.length
            ? 'Review your sessions'
            : 'Finish the episode account'
    const description = pendingSuggestions
      ? 'Choose the suggested relationship, or keep the episodes separate, before freezing this day.'
      : groupingInProgress
        ? 'Qwen is checking the day for over-fragmentation. You can keep reviewing episodes while it runs.'
        : unreconciledCount
          ? `${unreconciledCount} changed range${unreconciledCount === 1 ? '' : 's'} must be reconciled before this day can be finished.`
          : unstableEpisodes.length
            ? 'Check the times and evidence, then confirm the session or edit an episode.'
            : 'This confirms the whole day’s structure. Select episodes for memory separately; unrelated unfinished episodes do not block those selections.'

    return (
      <section id="episode-review-checkpoint" aria-live="polite" className={`rounded-lg border px-3 py-3 sm:px-4 ${reviewingSessions ? 'border-[var(--tape-line)] bg-[var(--tape-paper-raised)]' : 'border-[var(--tape-focus)] bg-[var(--tape-selected)]'}`}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-start gap-2">
              {pendingSuggestions ? <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" /> : groupingInProgress ? <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-[var(--tape-focus)]" /> : unreconciledCount ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" /> : <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-[var(--tape-focus)]" />}
              <div>
                <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
                <p className="mt-0.5 max-w-3xl text-xs leading-5 text-gray-600 dark:text-gray-300">{description}</p>
                {!pendingSuggestions && !groupingInProgress && !unreconciledCount && !reviewingSessions && referenceOnlyCount > 0 && (
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Media is reference-only unless you enable <strong>Remember content</strong> while editing its episode.</p>
                )}
                {!reviewingSessions && <CountSummary episodeCount={episodeCount} eligibleCount={eligibleCount} referenceOnlyCount={referenceOnlyCount} />}
              </div>
            </div>
          </div>
          <div className="shrink-0 sm:self-center">
            {pendingSuggestions ? (
              <Button size="sm" onClick={onReviewGrouping} icon={<ChevronRight className="h-4 w-4" />}>Review grouping</Button>
            ) : groupingInProgress ? (
              <Button size="sm" disabled icon={<Loader2 className="h-4 w-4 animate-spin" />}>Reviewing grouping…</Button>
            ) : reviewingSessions ? null : unreconciledCount ? (
              <Button size="sm" disabled>Waiting for reconciliation</Button>
            ) : (
              <Button size="sm" onClick={onFinish} disabled={finalizing} icon={finalizing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}>
                {finalizing ? 'Saving structural review…' : 'Finish structural review'}
              </Button>
            )}
          </div>
        </div>
        {!!unstableEpisodes.length && !pendingSuggestions && !groupingInProgress && !unreconciledCount && (
          <SessionStructureReview projection={projection} episodes={episodes} unstableEpisodes={unstableEpisodes} timezone={timezone} confirmingSessionId={confirmingSessionId} onConfirm={onConfirmStructures} onEdit={onEditEpisode} onNotActivity={onNotActivity} rejectingEpisodeId={rejectingEpisodeId} rejectionError={rejectionError} />
        )}
        {consolidation?.state === 'failed' && <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">Grouping suggestions failed. You can finish without them, or retry from Edit episodes.</p>}
        {confirmationError && <p className="mt-2 text-xs text-red-700 dark:text-red-300">Could not save this session review. {confirmationError.message}</p>}
        {error && <p className="mt-2 text-xs text-red-700 dark:text-red-300">Could not finish this episode review. {error.message}</p>}
      </section>
    )
  }

  if (review.state === 'memory_pending' || review.state === 'failed') {
    const failed = review.state === 'failed'
    return (
      <section id="episode-review-checkpoint" className={`flex flex-col gap-3 rounded-lg border px-3 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-4 ${failed ? 'border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/20' : 'border-[var(--tape-focus)] bg-[var(--tape-selected)]'}`}>
        <div>
          <h2 className={`text-sm font-semibold ${failed ? 'text-red-800 dark:text-red-200' : 'text-gray-900 dark:text-gray-100'}`}>{failed ? 'Potential memory needs attention' : 'Potential memory is ready'}</h2>
          <p className="mt-0.5 text-xs leading-5 text-gray-600 dark:text-gray-300">{failed ? review.error || 'The isolated extraction did not complete.' : 'Now decide which proposed changes, if any, may reach the accepted vault.'}</p>
        </div>
        <Link to={`/memory-ledger?view=review&date=${day}`} className="inline-flex min-h-8 shrink-0 items-center justify-center gap-1.5 rounded-md bg-[var(--tape-focus)] px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)] focus-visible:ring-offset-2">
          {failed ? 'Open memory review' : 'Review potential memory'} <ChevronRight className="h-4 w-4" />
        </Link>
      </section>
    )
  }

  if (review.state === 'memory_queued' || review.state === 'memory_generating' || review.state === 'memory_applying') {
    const generating = review.state === 'memory_generating'
    const applying = review.state === 'memory_applying'
    return (
      <section id="episode-review-checkpoint" aria-live="polite" className="flex items-start gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-3 sm:px-4">
        <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-[var(--tape-focus)]" />
        <div>
          <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{applying ? 'Applying the approved memory decision' : generating ? 'Extracting potential memory' : 'Potential memory is queued'}</h2>
          <p className="mt-0.5 text-xs leading-5 text-gray-600 dark:text-gray-300">{applying ? 'The selected proposal is being checked against the accepted vault before it lands.' : generating ? 'Extraction is running against a temporary vault. You can continue reviewing the timeline.' : 'Other dates and episodes do not block this memory selection.'}</p>
        </div>
      </section>
    )
  }

  return (
    <section id="episode-review-checkpoint" className="flex items-start gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-3 sm:px-4">
      <Check className="mt-0.5 h-4 w-4 shrink-0 text-green-700 dark:text-green-400" />
      <div>
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Day review complete</h2>
        <p className="mt-0.5 text-xs text-gray-600 dark:text-gray-300">{review.outcome === 'applied' ? 'Approved memory changes reached the vault.' : review.outcome === 'rejected' ? 'The potential memory was rejected.' : 'No vault changes were needed.'}</p>
      </div>
    </section>
  )
}
