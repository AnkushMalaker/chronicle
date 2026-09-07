// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, memorySpacesApi } from './api'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('memorySpacesApi', () => {
  it('allows semantic merge review to exceed the shared request timeout', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} })

    await memorySpacesApi.prepareMerge('space-1')

    expect(post).toHaveBeenCalledWith(
      '/api/spaces/space-1/merge-proposals',
      { acknowledge_sync_warnings: false },
      { timeout: 300_000 },
    )
  })
})
