import { useMemo, useState } from 'react'
import { AlertTriangle, GitMerge, Loader2, X } from 'lucide-react'
import { AuditConversation, dataAuditApi } from '../../services/api'
import { formatDate, formatDuration } from './format'

interface Props {
  conversations: AuditConversation[]
  onClose: () => void
  onDone: (message: string) => void
}

export default function MergePreviewModal({ conversations, onClose, onDone }: Props) {
  const [merging, setMerging] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const ordered = useMemo(
    () =>
      [...conversations].sort((a, b) =>
        (a.created_at || '').localeCompare(b.created_at || '')
      ),
    [conversations]
  )
  const totalDuration = ordered.reduce((acc, c) => acc + c.duration_seconds, 0)

  const confirmMerge = async () => {
    setMerging(true)
    setError(null)
    try {
      const res = await dataAuditApi.merge(ordered.map((c) => c.conversation_id))
      onDone(
        `Merged ${res.data.source_conversation_ids.length} conversations into one ` +
          `(${formatDuration(res.data.duration_seconds)}). Memory and title generation are queued.`
      )
      onClose()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to merge conversations')
      setMerging(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-xl max-h-[85vh] overflow-y-auto rounded-xl bg-white dark:bg-gray-800 shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center space-x-2">
            <GitMerge className="h-5 w-5 text-blue-600" />
            <h3 className="font-semibold text-gray-900 dark:text-gray-100">
              Merge {ordered.length} conversations
            </h3>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="px-6 py-4 space-y-4">
          {error && (
            <div className="flex items-center space-x-2 text-sm px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-200">
              <AlertTriangle className="h-4 w-4" />
              <span>{error}</span>
            </div>
          )}

          <div className="space-y-2">
            {ordered.map((c, i) => (
              <div
                key={c.conversation_id}
                className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200"
              >
                <div className="truncate">
                  <span className="text-gray-400 mr-2">{i + 1}.</span>
                  {c.title || c.conversation_id.slice(0, 8)}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap ml-3">
                  {formatDate(c.created_at)} · {formatDuration(c.duration_seconds)}
                </div>
              </div>
            ))}
          </div>

          <div className="text-sm text-gray-700 dark:text-gray-200">
            Combined duration: <strong>{formatDuration(totalDuration)}</strong>
          </div>

          <ul className="text-xs text-gray-500 dark:text-gray-400 list-disc pl-4 space-y-1">
            <li>
              Conversations must be adjacent — if another conversation from this device sits
              between them (even one filtered out of this view), the merge is rejected.
            </li>
            <li>
              Wall-clock gaps between the recordings are elided; a note marker in the transcript
              records each seam.
            </li>
            <li>
              The originals are soft-deleted (recoverable from Archive). Memories and the title
              are regenerated for the merged conversation.
            </li>
          </ul>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end space-x-2 px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-sm font-medium border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={confirmMerge}
            disabled={merging}
            className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {merging && <Loader2 className="h-4 w-4 animate-spin" />}
            <span>Merge</span>
          </button>
        </div>
      </div>
    </div>
  )
}
