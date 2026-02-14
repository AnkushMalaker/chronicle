import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { memoriesApi } from '../services/api'

export function useMemories(userId: string | undefined) {
  return useQuery({
    queryKey: ['memories', userId],
    queryFn: () => memoriesApi.getAll(userId).then(r => r.data),
    enabled: !!userId,
  })
}

export function useMemoryDetail(memoryId: string | undefined, userId: string | undefined) {
  return useQuery({
    queryKey: ['memory', memoryId],
    queryFn: () => memoriesApi.getById(memoryId!, userId).then(r => r.data.memory),
    enabled: !!memoryId && !!userId,
  })
}

export function useMemorySearch(query: string, userId?: string, limit?: number, scoreThreshold?: number) {
  return useQuery({
    queryKey: ['memories', 'search', query, userId, scoreThreshold],
    queryFn: () => memoriesApi.search(query, userId, limit, scoreThreshold).then(r => r.data),
    enabled: query.trim().length > 0,
  })
}

export function useDeleteMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => memoriesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memories'] })
    },
  })
}
