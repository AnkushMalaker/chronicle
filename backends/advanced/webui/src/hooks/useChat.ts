import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { chatApi } from '../services/api'

export function useChatSessions() {
  return useQuery({
    queryKey: ['chat', 'sessions'],
    queryFn: () => chatApi.getSessions().then(r => r.data),
  })
}

export function useChatMessages(sessionId: string | null) {
  return useQuery({
    queryKey: ['chat', 'messages', sessionId],
    queryFn: () => chatApi.getMessages(sessionId!).then(r => r.data),
    enabled: !!sessionId,
  })
}

export function useCreateChatSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (title?: string) => chatApi.createSession(title).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['chat', 'sessions'] })
    },
  })
}

export function useDeleteChatSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (sessionId: string) => chatApi.deleteSession(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['chat', 'sessions'] })
    },
  })
}

export function useExtractChatMemories() {
  return useMutation({
    mutationFn: (sessionId: string) => chatApi.extractMemories(sessionId).then(r => r.data),
  })
}
