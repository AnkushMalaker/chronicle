import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { finetuningApi } from '../services/api'

export function useFinetuningStatus() {
  return useQuery({
    queryKey: ['finetuning', 'status'],
    queryFn: () => finetuningApi.getStatus().then(r => r.data),
  })
}

export function useCronJobs() {
  return useQuery({
    queryKey: ['finetuning', 'cronJobs'],
    queryFn: () => finetuningApi.getCronJobs().then(r => r.data),
  })
}

export function useToggleCronJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ jobId, enabled }: { jobId: string; enabled: boolean }) =>
      finetuningApi.updateCronJob(jobId, { enabled }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finetuning'] })
    },
  })
}

export function useUpdateCronSchedule() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ jobId, schedule }: { jobId: string; schedule: string }) =>
      finetuningApi.updateCronJob(jobId, { schedule }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finetuning'] })
    },
  })
}

export function useRunCronJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => finetuningApi.runCronJob(jobId).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finetuning'] })
    },
  })
}

export function useProcessAnnotations() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (annotationType: string) => finetuningApi.processAnnotations(annotationType).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finetuning'] })
    },
  })
}

export function useDeleteOrphanedAnnotations() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (annotationType: string) => finetuningApi.deleteOrphanedAnnotations(annotationType).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finetuning'] })
    },
  })
}
