// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryReviewProposal, TimelineEpisode, timelineApi } from '../../services/api'
import MemorySelectionPanel from './MemorySelectionPanel'
import { CandidateChanges } from './ReviewDesk'

const request = {
  proposal_id: 'generation-one', request_id: 'request', generation: 1, state: 'pending', active: true,
  local_date: '2026-01-05', timezone: 'Asia/Kolkata', snapshot_id: 'a'.repeat(64),
  selected_episodes: [{ episode_key: 'one', revision: 2 }], replacement_proposal_id: null,
  supersedes_proposal_id: null, freshness: null, change_count: 1, accepted_change_ids: [], rejected_change_ids: [],
  error: null, created_at: '', generated_at: '', resolved_at: null,
  changes: [{ change_id: 'change-one', note_path: 'Topics/Plan.md', operation: 'create', before_hash: null, after_hash: 'hash', before_text: null, after_text: 'Historical plan', summary: 'Add January evidence', source_episode_keys: ['one'] }],
} as MemoryReviewProposal
const episodes = ['one', 'two'].map(key => ({ episode_key: key, episode_id: key, revision: 2, title: key, started_at: '2026-01-05T10:00:00Z', memory_policy: 'auto' })) as TimelineEpisode[]
const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>

afterEach(() => { cleanup(); vi.restoreAllMocks() })
beforeEach(() => {
  vi.spyOn(timelineApi, 'getMemorySelections').mockResolvedValue({ data: { proposals: [], outcomes: {} } } as never)
})

describe('selective memory review', () => {
  it('submits only selected revisions and leaves the other episode undecided', async () => {
    const submit = vi.spyOn(timelineApi, 'createMemorySelection').mockResolvedValue({ data: { proposals: [request] } } as never)
    render(<MemorySelectionPanel day="2026-01-05" timezone="Asia/Kolkata" snapshotId={'a'.repeat(64)} episodes={episodes} />, { wrapper })
    fireEvent.click(screen.getByText('Choose from 2 episodes'))
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByRole('button', { name: 'Review 1 selected episodes' }))
    await waitFor(() => expect(submit).toHaveBeenCalledWith('2026-01-05', 'Asia/Kolkata', 'a'.repeat(64), [{ episode_key: 'one', revision: 2 }]))
    expect(screen.getByText('two')).toBeInTheDocument()
  })
  it('requires a new checkbox decision when generation changes', () => {
    const { rerender } = render(<CandidateChanges proposal={request} day="2026-01-05" timezone="Asia/Kolkata" />, { wrapper })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('checkbox')).toBeChecked()
    rerender(<CandidateChanges proposal={{ ...request, proposal_id: 'generation-two', generation: 2 }} day="2026-01-05" timezone="Asia/Kolkata" />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByRole('button', { name: /Apply.*selected changes/ })).toBeDisabled()
  })
  it('posts the exact generation and selected change IDs for freshness checking', async () => {
    const resolve = vi.spyOn(timelineApi, 'resolveMemoryProposal').mockResolvedValue({ data: { outcome: 'checking', proposal: { ...request, state: 'checking' } } } as never)
    render(<CandidateChanges proposal={request} day="2026-01-05" timezone="Asia/Kolkata" />, { wrapper })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 selected changes' }))
    await waitFor(() => expect(resolve).toHaveBeenCalledWith('generation-one', 1, ['change-one']))
  })
})
