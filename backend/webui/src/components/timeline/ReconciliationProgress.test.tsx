// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import ReconciliationProgress, { JobProgress } from './ReconciliationProgress'

afterEach(cleanup)
const progress: JobProgress = {
  stage: 'context', message: 'Reading block 3 of 8', started_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  stages: [
    { id: 'photos', label: 'Inspect photos', state: 'completed', completed: 5, total: 5 },
    { id: 'context', label: 'Summarize context', state: 'running', completed: 2, total: 8, unit: 'blocks', attempt: 2 },
    { id: 'publication', label: 'Publish timeline', state: 'waiting' },
  ],
  events: [{ at: new Date().toISOString(), stage: 'context', message: 'Retrying block 3', state: 'running', attempt: 2 }],
}

it('shows actual unit progress and retries without claiming overall completion', () => {
  render(<ReconciliationProgress progress={progress} status="running" />)
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2')
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuemax', '8')
  expect(screen.getByText('2/8 blocks')).toBeVisible()
  expect(screen.getByText(/attempt 2/)).toBeVisible()
})

it('uses an indeterminate stage when the amount of work is unknown', () => {
  render(<ReconciliationProgress progress={{ ...progress, stages: [{ id: 'context', label: 'Summarize context', state: 'running' }] }} />)
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  expect(screen.getByText('Working…')).toBeVisible()
})

it('stops presenting a failed job as running', () => {
  render(<ReconciliationProgress progress={progress} status="failed" />)
  expect(screen.getByText('Reconciliation · failed')).toBeVisible()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})

it('distinguishes recovery pending from an active queue retry', () => {
  render(<ReconciliationProgress progress={{ ...progress, job_status: 'failed', message: 'Attempt failed; waiting for retry' }} status="queued" />)
  expect(screen.getByText('Reconciliation · Awaiting recovery')).toBeVisible()
  expect(screen.getByText('Attempt failed; awaiting recovery scheduler')).toBeVisible()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})

it('does not present queued work as actively processing', () => {
  render(<ReconciliationProgress progress={{ ...progress, job_status: 'scheduled' }} status="queued" />)
  expect(screen.getByText('Reconciliation · Queued')).toBeVisible()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})

it('replaces stale retry copy when the request is terminal', () => {
  render(<ReconciliationProgress progress={{ ...progress, job_status: 'failed', message: 'Attempt failed; waiting for retry' }} status="failed" />)
  expect(screen.getByText('Reconciliation failed')).toBeVisible()
  expect(screen.queryByText('Attempt failed; waiting for retry')).not.toBeInTheDocument()
})
