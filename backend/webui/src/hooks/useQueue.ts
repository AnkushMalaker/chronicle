import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { queueApi } from '../services/api'

export function useQueueDashboard(expandedSessions: string[]) {
  return useQuery({
    queryKey: ['queue', 'dashboard', expandedSessions],
    queryFn: () => queueApi.getDashboard(expandedSessions).then(r => r.data),
    // Expanding a conversation adds to the key, which would otherwise be a cold
    // query and blank the whole page to its loading spinner. Keep rendering the
    // previous result while the wider payload loads; `isFetching` still drives
    // the Refresh spinner.
    placeholderData: keepPreviousData,
  })
}

export function useQueueEvents(limit: number = 50, eventType?: string) {
  return useQuery({
    queryKey: ['queue', 'events', limit, eventType],
    queryFn: () => queueApi.getEvents(limit, eventType).then(r => r.data),
  })
}

export function useRetryJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ jobId, force }: { jobId: string; force?: boolean }) =>
      queueApi.retryJob(jobId, force),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] })
    },
  })
}

export function useCancelJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => queueApi.cancelJob(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] })
    },
  })
}

export function useFlushJobs() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ flushAll, body }: { flushAll: boolean; body: any }) =>
      queueApi.flushJobs(flushAll, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] })
    },
  })
}
