import { useEffect, useState } from 'react'
import { api } from '../../services/api'

export interface WaveformData {
  /** Peak amplitudes in [0, 1], evenly spaced at `sample_rate` peaks/sec over the whole clip. */
  samples: number[]
  /** Peaks per second (currently ~3/s from the backend; resolution of `samples`). */
  sample_rate: number
  duration_seconds: number
}

// Module-level cache so the playback waveform and the region editor on the same
// conversation share one fetch instead of each hitting /waveform.
const cache = new Map<string, WaveformData>()
const inflight = new Map<string, Promise<WaveformData>>()

/**
 * Fetch (and cache) the coarse full-clip waveform for a conversation.
 *
 * Intentionally minimal so we can later add a higher-resolution, range-scoped
 * variant without touching callers: add an optional `{ start, end, pps }` arg here,
 * key the cache on it, and hit a ranged endpoint — the returned shape stays the same.
 */
export function fetchWaveformData(conversationId: string): Promise<WaveformData> {
  const cached = cache.get(conversationId)
  if (cached) return Promise.resolve(cached)

  const existing = inflight.get(conversationId)
  if (existing) return existing

  const p = api
    .get(`/api/conversations/${conversationId}/waveform`)
    .then((res) => {
      const data = res.data as WaveformData
      cache.set(conversationId, data)
      inflight.delete(conversationId)
      return data
    })
    .catch((err) => {
      inflight.delete(conversationId)
      throw err
    })
  inflight.set(conversationId, p)
  return p
}

interface UseWaveformDataResult {
  data: WaveformData | null
  loading: boolean
  error: string | null
}

/** React hook wrapper around {@link fetchWaveformData}. */
export function useWaveformData(conversationId: string | undefined): UseWaveformDataResult {
  const [data, setData] = useState<WaveformData | null>(
    conversationId ? cache.get(conversationId) ?? null : null
  )
  const [loading, setLoading] = useState(!data)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!conversationId) return
    let cancelled = false

    const cached = cache.get(conversationId)
    if (cached) {
      setData(cached)
      setLoading(false)
      setError(null)
      return
    }

    setLoading(true)
    setError(null)
    fetchWaveformData(conversationId)
      .then((d) => {
        if (!cancelled) {
          setData(d)
          setLoading(false)
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setError(err?.response?.data?.detail || err?.message || 'Failed to load waveform')
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [conversationId])

  return { data, loading, error }
}
