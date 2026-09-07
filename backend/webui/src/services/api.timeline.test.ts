// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, timelineApi } from './api'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('timelineApi review actions', () => {
  it('posts an audited reason to the exact failed range', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} })

    await timelineApi.dismissFailedRange('range/one', 'Reviewed the evidence gap')

    expect(post).toHaveBeenCalledWith(
      '/api/timeline/reconciliation/ranges/range%2Fone/dismiss',
      { reason: 'Reviewed the evidence gap' },
    )
  })

  it('fences structure confirmation on snapshot, key, and revision', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} })

    await timelineApi.confirmEpisodeStructure(
      '2026-09-03',
      'Asia/Kolkata',
      'a'.repeat(64),
      'episode/key',
      7,
    )

    expect(post).toHaveBeenCalledWith(
      '/api/timeline/review/day/2026-09-03/episodes/episode%2Fkey/revisions/7/confirm-structure',
      { timezone: 'Asia/Kolkata', snapshot_id: 'a'.repeat(64) },
    )
  })
})
