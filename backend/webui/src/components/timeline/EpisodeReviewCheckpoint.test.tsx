// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { ComponentProps } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TimelineDayReview } from '../../services/api'
import EpisodeReviewCheckpoint from './EpisodeReviewCheckpoint'

const review: TimelineDayReview = {
  state: 'episodes_pending',
  review_snapshot_id: null,
  episodes_reviewed_at: null,
  resolved_at: null,
  outcome: null,
  error: null,
  proposal: null,
}

function renderCheckpoint(overrides: Partial<ComponentProps<typeof EpisodeReviewCheckpoint>> = {}) {
  const onReviewGrouping = vi.fn()
  const onDismissRange = vi.fn()
  const onConfirmStructures = vi.fn()
  const onFinish = vi.fn()
  render(
    <MemoryRouter>
      <EpisodeReviewCheckpoint
        day="2026-02-22"
        timezone="Asia/Kolkata"
        review={review}
        episodeCount={3}
        eligibleCount={0}
        referenceOnlyCount={3}
        unreconciledRanges={[]}
        unstableEpisodes={[]}
        episodes={[]}
        projection={{ version: 'test', day_started_at: '', day_ended_at: '', episode_count: 0, group_count: 0, needs_attention_count: 0, confirmed_count: 0, groups: [] }}
        onEditEpisode={vi.fn()}
        onNotActivity={vi.fn()}
        consolidation={null}
        finalizing={false}
        onReviewGrouping={onReviewGrouping}
        onDismissRange={onDismissRange}
        onConfirmStructures={onConfirmStructures}
        onFinish={onFinish}
        {...overrides}
      />
    </MemoryRouter>,
  )
  return { onReviewGrouping, onDismissRange, onConfirmStructures, onFinish }
}

afterEach(cleanup)

describe('EpisodeReviewCheckpoint', () => {
  it('explains the safe handoff and media eligibility before finishing', () => {
    const { onFinish } = renderCheckpoint()

    expect(screen.getByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    expect(screen.getByText(/Select episodes for memory separately/)).toBeVisible()
    expect(screen.getByText('0')).toBeVisible()
    expect(screen.getByText(/Media is reference-only/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Finish structural review' }))
    expect(onFinish).toHaveBeenCalledOnce()
  })

  it('requires a ready grouping suggestion to be decided first', () => {
    const { onReviewGrouping, onFinish } = renderCheckpoint({
      consolidation: {
        state: 'ready',
        snapshot_id: 'a'.repeat(64),
        model: 'qwen',
        suggestions: [{ suggestion_id: 'suggestion-1', episode_ids: ['one', 'two'], title: 'One activity', reason: 'Continuous work', confidence: 0.9 }],
      },
    })

    expect(screen.getByRole('heading', { name: 'Decide the suggested grouping' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Finish structural review' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review grouping' }))
    expect(onReviewGrouping).toHaveBeenCalledOnce()
    expect(onFinish).not.toHaveBeenCalled()
  })

  it('links to Memory Ledger only when an actual proposal is ready', () => {
    renderCheckpoint({ review: { ...review, state: 'memory_pending' } })

    expect(screen.getByRole('link', { name: /Review potential memory/ })).toHaveAttribute('href', '/memory-ledger?view=review&date=2026-02-22')
  })
})
