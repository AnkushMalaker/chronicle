// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../contexts/AuthContext'
import { authApi, memoryApi, timelineApi } from '../services/api'
import MemoryLedger from './MemoryLedger'

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('Memory Ledger review workspace', () => {
  it('opens a URL-addressable review day without silently changing timezone', async () => {
    localStorage.setItem('root_token', 'test-token')
    vi.spyOn(authApi, 'getMe').mockResolvedValue({ data: {
      id: 'user-1',
      email: 'user@example.com',
      display_name: 'User',
      assistant_name: null,
      timezone: 'Asia/Calcutta',
      is_superuser: true,
    } } as never)
    vi.spyOn(memoryApi, 'getAudit').mockResolvedValue({ data: { entries: [], total: 0 } } as never)
    vi.spyOn(timelineApi, 'getReviewQueue').mockResolvedValue({ data: { items: [] } } as never)
    vi.spyOn(timelineApi, 'getDay').mockResolvedValue({ data: {
      date: '2026-02-19',
      timezone: 'Asia/Calcutta',
      active_run_id: null,
      coverage: { unassigned_intervals: [] },
      analysis: null,
      consolidation: null,
      semantic_groups: [],
      review_decision_count: 0,
      review_projection: { version: 'test', day_started_at: '2026-02-18T18:30:00.000Z', day_ended_at: '2026-02-19T18:30:00.000Z', episode_count: 0, group_count: 0, needs_attention_count: 0, confirmed_count: 0, groups: [] },
      review: null,
      reconciliation: { ranges: [] },
      episodes: [],
    } } as never)
    const setTimezone = vi.spyOn(timelineApi, 'setTimezone').mockResolvedValue({ data: { timezone: 'UTC' } } as never)

    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AuthProvider>
          <MemoryRouter initialEntries={['/memory-ledger?view=review&date=2026-02-19']}>
            <MemoryLedger />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('tab', { name: 'Review queue' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('link', { name: 'Open Timeline for Feb 19' })).toHaveAttribute('href', '/timeline?date=2026-02-19')
    expect(setTimezone).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Use browser timezone' }))
    await waitFor(() => expect(setTimezone).toHaveBeenCalledWith(Intl.DateTimeFormat().resolvedOptions().timeZone))
  })
})
