import { describe, expect, it } from 'vitest'
import { TimelineReviewQueueItem } from '../../services/api'
import { buildReviewCursor, reviewAction } from './reviewCursor'

function item(
  date: string,
  state: TimelineReviewQueueItem['state'],
  extra: Partial<TimelineReviewQueueItem> = {},
): TimelineReviewQueueItem {
  return {
    date,
    state,
    outcome: null,
    episode_count: 4,
    unexplained_count: 0,
    capture_gap_count: 0,
    proposal: null,
    ...extra,
  }
}

describe('review cursor', () => {
  it('chooses the oldest day awaiting episode review and stays in Timeline', () => {
    const cursor = buildReviewCursor([
      item('2026-02-18', 'finalized'),
      item('2026-02-19', 'memory_generating'),
      item('2026-02-20', 'memory_pending'),
      item('2026-02-21', 'episodes_pending'),
    ])

    expect(cursor.unresolvedCount).toBe(1)
    expect(cursor.next?.item.date).toBe('2026-02-21')
    expect(cursor.next?.label).toBe('Review episodes')
    expect(cursor.next?.href).toBe('/timeline?date=2026-02-21')
  })

  it('keeps episode review on Timeline and calls out coverage attention', () => {
    const action = reviewAction(item('2026-07-26', 'episodes_pending', {
      unexplained_count: 3,
      capture_gap_count: 2,
    }))

    expect(action.href).toBe('/timeline?date=2026-07-26')
    expect(action.label).toBe('Review coverage and episodes')
    expect(action.attentionLabel).toBe('3 unexplained · 2 capture gaps')
  })

  it('does not turn memory workflow states into Timeline navigation', () => {
    const cursor = buildReviewCursor([
      item('2026-02-19', 'memory_queued'),
      item('2026-02-20', 'memory_applying'),
    ])

    expect(cursor.unresolvedCount).toBe(0)
    expect(cursor.next).toBeNull()
  })
})
