// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { memorySpacesApi } from '../services/api'
import MemorySpaceWorkspace from './MemorySpaceWorkspace'

vi.mock('./LiveRecord', () => ({
  default: ({ memorySpaceId, destinationLabel }: { memorySpaceId?: string; destinationLabel?: string }) => (
    <div data-testid="scoped-recorder">{memorySpaceId}:{destinationLabel}</div>
  ),
}))

const SPACE_ID = '9f3523c8-af75-469d-995a-7179531f3fc8'

function renderWorkspace(path = `/spaces/${SPACE_ID}/record`) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path="/spaces/:spaceId/:tab?" element={<MemorySpaceWorkspace />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MemorySpaceWorkspace scope', () => {
  it('keeps the seal and recording destination visible before capture', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Launch brainstorm',
        state: 'active',
        seed_notes: [],
        sync_state: 'healthy',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-29T00:00:00Z',
        updated_at: '2026-08-29T00:00:00Z',
        archived_at: null,
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({
      data: { conversations: [], total: 0 },
    } as never)

    renderWorkspace()

    expect(await screen.findByText('Main sealed')).toBeVisible()
    expect(screen.getByText('Recordings land here')).toBeVisible()
    expect(screen.getByTestId('scoped-recorder')).toHaveTextContent(`${SPACE_ID}:Launch brainstorm`)
    expect(screen.getByRole('link', { name: 'Notes' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Chat' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Merge' })).toBeVisible()
  })

  it('shows an archived vault as read-only with an explicit reopen action', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Past cycle',
        state: 'archived',
        seed_notes: [],
        sync_state: 'frozen',
        sync_error: null,
        merge_checkpoint: 'hash',
        created_at: '2026-08-29T00:00:00Z',
        updated_at: '2026-08-29T00:00:00Z',
        archived_at: '2026-08-29T01:00:00Z',
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({ data: { conversations: [] } } as never)
    vi.spyOn(memorySpacesApi, 'notes').mockResolvedValue({ data: [] } as never)

    renderWorkspace(`/spaces/${SPACE_ID}/notes`)

    expect(await screen.findByRole('button', { name: 'Reopen new cycle' })).toBeVisible()
    expect(screen.getByText(/Archived spaces are read-only/i)).toBeVisible()
  })

  it('sends only checked ScreenPipe frames into explicit note extraction', async () => {
    const conversationId = 'conversation-1'
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Rainbow ramble',
        state: 'active',
        seed_notes: [],
        sync_state: 'healthy',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-30T05:00:00Z',
        updated_at: '2026-08-30T05:00:00Z',
        archived_at: null,
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({
      data: { conversations: [{ conversation_id: conversationId, title: 'Career notebook', processing_status: 'completed', memory_review_state: 'ready' }] },
    } as never)
    vi.spyOn(memorySpacesApi, 'noteReview').mockResolvedValue({
      data: {
        conversation_id: conversationId,
        title: 'Career notebook',
        transcript: 'Let me show you the four aspects in my notebook.',
        created_at: '2026-08-30T05:15:00Z',
        started_at: '2026-08-30T05:15:00Z',
        ended_at: '2026-08-30T05:18:00Z',
        review_state: 'ready',
        review_error: null,
        selected_frame_keys: [],
        context_description: null,
        sources: [{ source_id: 'screenpipe-rainbow', name: 'Rainbow', platform: 'linux', status: 'online', last_seen_at: '2026-08-30T05:19:00Z', health: {} }],
        jobs: [],
        frames: [
          { key: 'screenpipe-rainbow:41', source_id: 'screenpipe-rainbow', frame_id: 41, captured_at: '2026-08-30T05:16:00Z', content_type: 'image/jpeg' },
          { key: 'screenpipe-rainbow:42', source_id: 'screenpipe-rainbow', frame_id: 42, captured_at: '2026-08-30T05:17:00Z', content_type: 'image/jpeg' },
        ],
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'noteReviewFrame').mockResolvedValue({ data: new Blob(['frame'], { type: 'image/jpeg' }) } as never)
    vi.spyOn(memorySpacesApi, 'notes').mockResolvedValue({ data: [] } as never)
    const extract = vi.spyOn(memorySpacesApi, 'extractReviewedNote').mockResolvedValue({ data: {} } as never)
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:frame'), revokeObjectURL: vi.fn() })

    renderWorkspace()

    expect(await screen.findByText('Choose what the note extractor can see')).toBeVisible()
    expect(screen.getByText(/Only checked frames are sent as pixels/i)).toBeVisible()
    const checks = screen.getAllByRole('checkbox')
    fireEvent.click(checks[1])
    fireEvent.click(screen.getByRole('button', { name: 'Extract note with 1 image' }))

    await waitFor(() => expect(extract).toHaveBeenCalledWith(
      SPACE_ID,
      conversationId,
      ['screenpipe-rainbow:42'],
    ))
    // Let the mutation's post-success refetches settle before afterEach restores
    // the API spies; otherwise an in-flight query can fall through to localhost.
    await waitFor(() => {
      expect(memorySpacesApi.noteReview).toHaveBeenCalledTimes(2)
      expect(memorySpacesApi.recordings).toHaveBeenCalledTimes(2)
    })
  })
})
