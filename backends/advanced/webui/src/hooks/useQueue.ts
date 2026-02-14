import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { queueApi } from '../services/api'

export function useQueueDashboard(expandedSessions: string[], autoRefresh: boolean) {
  return useQuery({
    queryKey: ['queue', 'dashboard', expandedSessions],
    queryFn: () => queueApi.getDashboard(expandedSessions).then(r => r.data),
    refetchInterval: autoRefresh ? 5000 : false,
  })
}

export function useQueueEvents(limit: number = 50, eventType?: string) {
  return useQuery({
    queryKey: ['queue', 'events', limit, eventType],
    queryFn: () => queueApi.getEvents(limit, eventType).then(r => r.data),
    staleTime: 10_000,
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

export function useCleanupStuckWorkers() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => queueApi.cleanupStuckWorkers(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] })
    },
  })
}

export function useCleanupOldSessions() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (maxAgeSeconds?: number) => queueApi.cleanupOldSessions(maxAgeSeconds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] })
    },
  })
}
