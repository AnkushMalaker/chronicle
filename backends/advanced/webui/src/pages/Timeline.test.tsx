// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../contexts/AuthContext'
import { authApi, TimelineDay, timelineApi } from '../services/api'
import Timeline from './Timeline'

const todayInKolkata = () => {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const value = Object.fromEntries(parts.map(part => [part.type, part.value]))
  return `${value.year}-${value.month}-${value.day}`
}

const TEST_DAY = todayInKolkata()

const emptyDay = {
  date: TEST_DAY,
  timezone: 'Asia/Calcutta',
  active_run_id: null,
  coverage: { unassigned_intervals: [] },
  analysis: null,
  consolidation: null,
  semantic_groups: [],
  review_decision_count: 0,
  review_projection: {
    version: 'test',
    day_started_at: '2026-08-27T18:30:00.000Z',
    day_ended_at: '2026-08-28T18:30:00.000Z',
    episode_count: 0,
    group_count: 0,
    needs_attention_count: 0,
    confirmed_count: 0,
    groups: [],
  },
  review: {
    state: 'episodes_pending',
    review_run_id: null,
    episodes_reviewed_at: null,
    resolved_at: null,
    outcome: null,
    error: null,
    proposal: null,
  },
  reconciliation: { ranges: [] },
  episodes: [],
} satisfies TimelineDay

const reviewableDay = {
  ...emptyDay,
  active_run_id: 'run-1',
  review_projection: {
    ...emptyDay.review_projection,
    episode_count: 1,
    group_count: 1,
    groups: [{
      group_id: 'session-1',
      started_at: '2026-08-28T09:00:00.000Z',
      ended_at: '2026-08-28T09:30:00.000Z',
      title: 'Planning Chronicle work',
      summary: 'Reviewed the implementation plan.',
      semantic: false,
      lane: 'conversation' as const,
      episode_ids: ['episode-1'],
      episode_count: 1,
      conversational_count: 1,
      confirmed_count: 0,
      duration_seconds: 1800,
      span_seconds: 1800,
      gap_seconds: 0,
      intervals: [{ episode_id: 'episode-1', started_at: '2026-08-28T09:00:00.000Z', ended_at: '2026-08-28T09:30:00.000Z' }],
      entities: [],
      salience: 'routine' as const,
      attention_reasons: [],
      needs_attention: false,
    }],
  },
  episodes: [{
    episode_id: 'episode-1',
    episode_key: 'episode-key-1',
    started_at: '2026-08-28T09:00:00.000Z',
    ended_at: '2026-08-28T09:30:00.000Z',
    kind: 'conversation',
    title: 'Planning Chronicle work',
    summary: 'Reviewed the implementation plan.',
    status: 'provisional' as const,
    confirmed_at: null,
    confirmed_fields: [],
    memory_policy: 'auto' as const,
    salience: 'routine' as const,
    confidence: 0.91,
    activity_mode: 'foreground' as const,
    entities: [],
    attributes: {},
    assertions: [],
    evidence: [],
    related_episode_ids: [],
    related_conversation_ids: [],
    audio_ranges: [],
    parent_episode_id: null,
    has_thumbnail: false,
  }],
} satisfies TimelineDay

function renderTimeline(analysis: TimelineDay['analysis'], dayData: TimelineDay = emptyDay) {
  localStorage.setItem('root_token', 'test-token')
  vi.spyOn(authApi, 'getMe').mockResolvedValue({ data: {
    id: 'user-1',
    email: 'user@example.com',
    display_name: 'User',
    assistant_name: null,
    timezone: 'Asia/Calcutta',
    is_superuser: true,
  } } as never)
  vi.spyOn(timelineApi, 'getDay').mockResolvedValue({ data: { ...dayData, analysis } } as never)
  vi.spyOn(timelineApi, 'getReviewQueue').mockResolvedValue({ data: { items: [{
    date: '2026-02-19',
    state: 'episodes_pending',
    outcome: null,
    episode_count: 32,
    unexplained_count: 0,
    capture_gap_count: 0,
    proposal: null,
  }] } } as never)
  const reconciliation = {
    request_id: 'request-one',
    date: TEST_DAY,
    timezone: 'Asia/Calcutta',
    pipeline: 'day',
    state: 'blocked',
    reason: 'no_immich_evidence',
    target_asset_count: 0,
    latest_eligible_asset_date: null,
    checked_at: '2026-08-28T00:00:00.000Z',
    notification_id: 'notice-one',
    notification_status: 'queued',
    job_id: null,
    dirty_range_id: null,
    run_id: null,
    last_error: null,
    created_at: '2026-08-28T00:00:00.000Z',
    updated_at: '2026-08-28T00:00:00.000Z',
  }
  const reconcile = vi.spyOn(timelineApi, 'reconcileDay').mockResolvedValue({ data: reconciliation } as never)
  vi.spyOn(timelineApi, 'getReconciliation').mockResolvedValue({ data: reconciliation } as never)

  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <MemoryRouter initialEntries={[`/timeline?date=${TEST_DAY}`]}>
          <Timeline />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
  return reconcile
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('Timeline next action', () => {
  it('turns an unprocessed empty day into an explicit review or reconciliation choice', async () => {
    const reconcile = renderTimeline(null)

    expect(await screen.findByRole('heading', { name: 'Today has no processed episodes yet.' })).toBeVisible()
    expect(screen.getByRole('link', { name: /Continue review/i })).toHaveAttribute('href', '/timeline?date=2026-02-19')
    expect(screen.getByText('Next: Feb 19 · Review episodes')).toBeVisible()
    expect(screen.queryByText('Episodes await review')).not.toBeInTheDocument()
    expect(reconcile).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile this day' }))
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta'))
  })

  it('restores durable reconciliation status after a reload', async () => {
    localStorage.setItem(
      `chronicle.timeline.reconciliation:Asia/Calcutta:${TEST_DAY}`,
      'request-one',
    )
    renderTimeline(null)

    await waitFor(() => {
      expect(timelineApi.getReconciliation).toHaveBeenCalledWith('request-one')
    })
    expect(await screen.findByText('Reconciliation blocked')).toBeVisible()
    expect(screen.getByText('Backup reminder: queued')).toBeVisible()
  })

  it('keeps the Timeline review cursor actionable while today is processing', async () => {
    const reconcile = renderTimeline({
      run_id: 'run-1',
      state: 'pending',
      attempts: 0,
      retry_after: null,
      error: null,
      created_at: '2026-08-28T00:00:00.000Z',
      completed_at: null,
    })

    expect(await screen.findByRole('heading', { name: 'Today’s episodes are still processing.' })).toBeVisible()
    expect(screen.getByRole('link', { name: /Continue review/i })).toHaveAttribute('href', '/timeline?date=2026-02-19')
    expect(screen.queryByRole('button', { name: 'Reconcile this day' })).not.toBeInTheDocument()
    expect(reconcile).not.toHaveBeenCalled()
  })

  it('finishes episode review on Timeline without jumping to Memory Ledger', async () => {
    const finalize = vi.spyOn(timelineApi, 'finalizeEpisodes').mockResolvedValue({ data: { ...reviewableDay.review!, state: 'memory_queued' } } as never)
    renderTimeline(null, reviewableDay)

    expect(await screen.findByRole('heading', { name: 'Finish the episode account' })).toBeVisible()
    expect(screen.getByText('memory-eligible').parentElement).toHaveTextContent('1 memory-eligible')
    expect(screen.queryByRole('link', { name: /Memory queued|Extracting potential memory/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Finish episode review' }))
    await waitFor(() => expect(finalize).toHaveBeenCalledWith(TEST_DAY, 'Asia/Calcutta'))
  })
})
