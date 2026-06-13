import { useCallback, useEffect, useRef } from 'react'
import { BatchProgress, dataAuditApi } from '../services/api'

/**
 * Poll an RQ job until it finishes or fails.
 *
 * Returns a `pollJob` function resolving with the terminal status
 * ('finished' | 'failed'). The interval is cleaned up on unmount; transient
 * polling errors are tolerated.
 */
export function useJobPolling(intervalMs: number = 2000) {
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const pollJob = useCallback(
    (jobId: string, onStatus?: (status: string, progress?: BatchProgress) => void) =>
      new Promise<'finished' | 'failed'>((resolve) => {
        if (pollRef.current) clearInterval(pollRef.current)
        pollRef.current = setInterval(async () => {
          try {
            const res = await dataAuditApi.getJobStatus(jobId)
            const status = res.data.status
            if (status === 'finished' || status === 'failed') {
              if (pollRef.current) clearInterval(pollRef.current)
              pollRef.current = null
              resolve(status)
            } else if (onStatus) {
              onStatus(status, res.data.batch_progress)
            }
          } catch {
            // keep polling; transient errors are tolerated
          }
        }, intervalMs)
      }),
    [intervalMs]
  )

  return { pollJob }
}
