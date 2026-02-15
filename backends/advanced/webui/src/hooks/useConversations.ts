import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { conversationsApi } from '../services/api'

interface ConversationListOpts {
  includeDeleted?: boolean
  includeUnprocessed?: boolean
}

export function useConversations(opts: ConversationListOpts = {}) {
  return useQuery({
    queryKey: ['conversations', opts],
    queryFn: () => conversationsApi.getAll(opts.includeDeleted, opts.includeUnprocessed).then(r => r.data),
  })
}

export function useConversationMemories(conversationId: string | null) {
  return useQuery({
    queryKey: ['conversationMemories', conversationId],
    queryFn: () => conversationsApi.getMemories(conversationId!).then(r => r.data),
    enabled: !!conversationId,
  })
}

export function useConversationDetail(conversationId: string | null) {
  return useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () => conversationsApi.getById(conversationId!).then(r => r.data.conversation),
    enabled: !!conversationId,
  })
}

export function useDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function usePermanentDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.permanentDelete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useRestoreConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.restore(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessTranscript() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (conversationId: string) => conversationsApi.reprocessTranscript(conversationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ conversationId, transcriptVersionId }: { conversationId: string; transcriptVersionId?: string }) =>
      conversationsApi.reprocessMemory(conversationId, transcriptVersionId || 'active'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessSpeakers() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ conversationId, transcriptVersionId }: { conversationId: string; transcriptVersionId?: string }) =>
      conversationsApi.reprocessSpeakers(conversationId, transcriptVersionId || 'active'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessOrphan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (conversationId: string) => conversationsApi.reprocessOrphan(conversationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}
