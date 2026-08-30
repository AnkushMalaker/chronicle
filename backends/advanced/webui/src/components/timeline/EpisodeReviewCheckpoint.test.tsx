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
  review_run_id: null,
  episodes_reviewed_at: null,
  resolved_at: null,
  outcome: null,
  error: null,
  proposal: null,
}

function renderCheckpoint(overrides: Partial<ComponentProps<typeof EpisodeReviewCheckpoint>> = {}) {
  const onReviewGrouping = vi.fn()
  const onFinish = vi.fn()
  render(
    <MemoryRouter>
      <EpisodeReviewCheckpoint
        day="2026-02-22"
        review={review}
        episodeCount={3}
        eligibleCount={0}
        referenceOnlyCount={3}
        unreconciledCount={0}
        consolidation={null}
        finalizing={false}
        onReviewGrouping={onReviewGrouping}
        onFinish={onFinish}
        {...overrides}
      />
    </MemoryRouter>,
  )
  return { onReviewGrouping, onFinish }
}

afterEach(cleanup)

describe('EpisodeReviewCheckpoint', () => {
  it('explains the safe handoff and media eligibility before finishing', () => {
    const { onFinish } = renderCheckpoint()

    expect(screen.getByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    expect(screen.getByText(/Nothing reaches the vault until you approve it/)).toBeVisible()
    expect(screen.getByText('0')).toBeVisible()
    expect(screen.getByText(/Media is reference-only/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Finish episode review' }))
    expect(onFinish).toHaveBeenCalledOnce()
  })

  it('requires a ready grouping suggestion to be decided first', () => {
    const { onReviewGrouping, onFinish } = renderCheckpoint({
      consolidation: {
        state: 'ready',
        run_id: 'group-run',
        model: 'qwen',
        suggestions: [{ suggestion_id: 'suggestion-1', episode_ids: ['one', 'two'], title: 'One activity', reason: 'Continuous work', confidence: 0.9 }],
      },
    })

    expect(screen.getByRole('heading', { name: 'Decide the suggested grouping' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Finish episode review' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review grouping' }))
    expect(onReviewGrouping).toHaveBeenCalledOnce()
    expect(onFinish).not.toHaveBeenCalled()
  })

  it('links to Memory Ledger only when an actual proposal is ready', () => {
    renderCheckpoint({ review: { ...review, state: 'memory_pending' } })

    expect(screen.getByRole('link', { name: /Review potential memory/ })).toHaveAttribute('href', '/memory-ledger?view=review&date=2026-02-22')
  })
})
