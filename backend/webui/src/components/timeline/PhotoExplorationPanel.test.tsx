// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { timelineApi } from '../../services/api'
import PhotoExplorationPanel from './PhotoExplorationPanel'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('loads private grids on demand and reports unseen photos without implying full coverage', async () => {
  const details = vi.spyOn(timelineApi, 'getPhotoExploration').mockResolvedValue({ data: {
    coverage: { inventory_count: 100, inspected_count: 12, unseen_count: 88, round_count: 1, stop_reason: 'budget_exhausted' },
    rounds: [{ round: 1, action: 'overview', offered: ['one'], question: 'What happened in this interval?' }],
  } } as never)
  vi.spyOn(timelineApi, 'getPhotoExplorationGrid').mockResolvedValue({ data: new Blob(['png']) } as never)
  URL.createObjectURL = vi.fn(() => 'blob:private-grid')
  URL.revokeObjectURL = vi.fn()
  const view = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><PhotoExplorationPanel requestId="request-one" /></QueryClientProvider>)
  expect(details).not.toHaveBeenCalled()
  const disclosure = screen.getByText('Photo coverage and model input grids').closest('details')!
  disclosure.open = true
  fireEvent(disclosure, new Event('toggle'))
  expect(await screen.findByText(/12 of 100 photos inspected/)).toBeInTheDocument()
  expect(screen.getByText(/88 unseen/)).toBeInTheDocument()
  expect(screen.getByText(/budget exhausted/)).toBeInTheDocument()
  expect(await screen.findByAltText('Model input photo grid, round 1')).toHaveAttribute('src', 'blob:private-grid')
  expect(timelineApi.getPhotoExplorationGrid).toHaveBeenCalledWith('request-one', 1)
  view.unmount()
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:private-grid'))
})
