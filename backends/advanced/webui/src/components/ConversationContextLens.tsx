import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AppWindow, Image, Loader2, RefreshCw, Save, Trash2 } from 'lucide-react'
import { deviceInputApi, DeviceInputItem } from '../services/api'
import { Button, Card, IconButton } from './ui'

function label(item: DeviceInputItem) {
  return item.metadata.app_name || item.metadata.window_name || item.metadata.text || item.kind
}

export default function ConversationContextLens({ conversationId }: { conversationId: string }) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['conversation-context', conversationId],
    queryFn: async () => (await deviceInputApi.getConversationContext(conversationId)).data.items,
    refetchInterval: 10_000,
  })
  const request = useMutation({
    mutationFn: () => deviceInputApi.requestConversationContext(conversationId),
    onSuccess: () => setTimeout(() => queryClient.invalidateQueries({ queryKey: ['conversation-context', conversationId] }), 1500),
  })
  const clear = useMutation({
    mutationFn: () => deviceInputApi.clearConversationContext(conversationId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['conversation-context', conversationId] }),
  })
  const promote = useMutation({
    mutationFn: (itemId: string) => deviceInputApi.promoteItem(itemId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['conversation-context', conversationId] }),
  })
  const items = query.data || []

  return (
    <Card raised padded={false} className="p-6">
      <div className="flex items-center justify-between gap-3 mb-4">
        <div>
          <h2 className="font-medium text-gray-900 dark:text-gray-100">Conversation Lens</h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Screen and photo context from the same time window.</p>
        </div>
        <div className="flex gap-2">
          {items.length > 0 && (
            <IconButton onClick={() => clear.mutate()} disabled={clear.isPending} label="Clear non-vault context">
              <Trash2 className="w-4 h-4" />
            </IconButton>
          )}
          <Button
            variant="primary"
            onClick={() => request.mutate()}
            disabled={request.isPending}
            icon={request.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          >
            Find context
          </Button>
        </div>
      </div>
      {query.isLoading ? (
        <div className="text-sm text-gray-500">Loading context…</div>
      ) : items.length === 0 ? (
        <div className="border border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-5 text-sm text-gray-500 dark:text-gray-400">
          No linked context yet. Chronicle will ask connected sources only for this conversation's bounded time range.
        </div>
      ) : (
        <div className="space-y-2">
          {items.map(item => (
            <div key={item.id} className="flex items-start gap-3 rounded-lg bg-gray-50 dark:bg-gray-900/40 p-3">
              {item.kind === 'immich_memory' ? <Image className="w-4 h-4 mt-0.5 text-purple-500" /> : <AppWindow className="w-4 h-4 mt-0.5 text-blue-500" />}
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{label(item)}</div>
                <div className="text-xs text-gray-500">{new Date(item.captured_at).toLocaleString()} · {item.source_id}</div>
                {item.metadata.text && <p className="text-xs text-gray-600 dark:text-gray-300 mt-1 line-clamp-3">{item.metadata.text}</p>}
              </div>
              {item.kind === 'immich_memory' && item.state !== 'promoted' && (
                <Button
                  variant="secondary"
                  onClick={() => promote.mutate(item.id)}
                  disabled={promote.isPending}
                  title="Copy this photo into the vault"
                  icon={<Save className="w-3.5 h-3.5" />}
                >
                  Vault
                </Button>
              )}
              {item.state === 'promoted' && <span className="text-xs text-green-600">In vault</span>}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
