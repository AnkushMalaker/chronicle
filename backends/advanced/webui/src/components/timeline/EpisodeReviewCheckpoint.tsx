import { AlertTriangle, Check, ChevronRight, Loader2, LockKeyhole, Sparkles } from 'lucide-react'
import { Link } from 'react-router-dom'
import { TimelineConsolidationProposal, TimelineDayReview } from '../../services/api'
import { Button } from '../ui'

interface EpisodeReviewCheckpointProps {
  day: string
  review: TimelineDayReview
  episodeCount: number
  eligibleCount: number
  referenceOnlyCount: number
  unreconciledCount: number
  consolidation: TimelineConsolidationProposal | null
  finalizing: boolean
  error?: Error | null
  onReviewGrouping: () => void
  onFinish: () => void
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
  review,
  episodeCount,
  eligibleCount,
  referenceOnlyCount,
  unreconciledCount,
  consolidation,
  finalizing,
  error,
  onReviewGrouping,
  onFinish,
}: EpisodeReviewCheckpointProps) {
  const pendingSuggestions = consolidation?.state === 'ready' && consolidation.suggestions.length > 0
  const groupingInProgress = consolidation?.state === 'queued' || consolidation?.state === 'generating'

  if (review.state === 'episodes_pending') {
    const title = pendingSuggestions
      ? 'Decide the suggested grouping'
      : groupingInProgress
        ? 'Grouping review is running'
        : unreconciledCount
          ? 'Episode review is waiting for reconciliation'
          : 'Finish the episode account'
    const description = pendingSuggestions
      ? 'Choose the suggested relationship, or keep the episodes separate, before freezing this day.'
      : groupingInProgress
        ? 'Qwen is checking the day for over-fragmentation. You can keep reviewing episodes while it runs.'
        : unreconciledCount
          ? `${unreconciledCount} changed range${unreconciledCount === 1 ? '' : 's'} must be reconciled before this day can be finished.`
          : 'This freezes the episode account and queues an isolated potential-memory proposal. Nothing reaches the vault until you approve it.'

    return (
      <section id="episode-review-checkpoint" aria-live="polite" className="rounded-lg border border-[var(--tape-focus)] bg-[var(--tape-selected)] px-3 py-3 sm:px-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-start gap-2">
              {pendingSuggestions ? <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" /> : groupingInProgress ? <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-[var(--tape-focus)]" /> : unreconciledCount ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" /> : <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-[var(--tape-focus)]" />}
              <div>
                <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
                <p className="mt-0.5 max-w-3xl text-xs leading-5 text-gray-600 dark:text-gray-300">{description}</p>
                {!pendingSuggestions && !groupingInProgress && !unreconciledCount && referenceOnlyCount > 0 && (
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Media is reference-only unless you enable <strong>Remember content</strong> while editing its episode.</p>
                )}
                <CountSummary episodeCount={episodeCount} eligibleCount={eligibleCount} referenceOnlyCount={referenceOnlyCount} />
              </div>
            </div>
          </div>
          <div className="shrink-0 sm:self-center">
            {pendingSuggestions ? (
              <Button size="sm" onClick={onReviewGrouping} icon={<ChevronRight className="h-4 w-4" />}>Review grouping</Button>
            ) : groupingInProgress ? (
              <Button size="sm" disabled icon={<Loader2 className="h-4 w-4 animate-spin" />}>Reviewing grouping…</Button>
            ) : unreconciledCount ? (
              <Button size="sm" disabled>Waiting for reconciliation</Button>
            ) : (
              <Button size="sm" onClick={onFinish} disabled={finalizing} icon={finalizing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}>
                {finalizing ? 'Queuing potential memory…' : 'Finish episode review'}
              </Button>
            )}
          </div>
        </div>
        {consolidation?.state === 'failed' && <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">Grouping suggestions failed. You can finish without them, or retry from Edit episodes.</p>}
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
          <p className="mt-0.5 text-xs leading-5 text-gray-600 dark:text-gray-300">{applying ? 'The selected proposal is being checked against the accepted vault before it lands.' : generating ? 'Extraction is running against a temporary vault. You can continue reviewing the timeline.' : 'Chronological review may wait for an earlier day. You can continue reviewing the timeline.'}</p>
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
