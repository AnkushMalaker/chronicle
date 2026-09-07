import { TimelineReviewQueueItem } from '../../services/api'

export interface ReviewCursorAction {
  item: TimelineReviewQueueItem
  href: string
  label: string
  attentionLabel: string | null
}

function countLabel(count: number, singular: string, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`
}

export function reviewAction(item: TimelineReviewQueueItem): ReviewCursorAction {
  const coverageParts = [
    item.unexplained_count ? `${item.unexplained_count} unexplained` : null,
    item.capture_gap_count ? countLabel(item.capture_gap_count, 'capture gap') : null,
  ].filter((value): value is string => Boolean(value))

  return {
    item,
    href: `/timeline?date=${item.date}`,
    label: coverageParts.length ? 'Review coverage and episodes' : 'Review episodes',
    attentionLabel: coverageParts.length ? coverageParts.join(' · ') : null,
  }
}

export function buildReviewCursor(items: TimelineReviewQueueItem[]) {
  const awaitingEpisodeReview = [...items]
    .filter(item => item.state === 'episodes_pending')
    .sort((left, right) => left.date.localeCompare(right.date))
  const actions = awaitingEpisodeReview.map(reviewAction)
  return {
    unresolvedCount: awaitingEpisodeReview.length,
    next: actions[0] || null,
    actions,
  }
}
