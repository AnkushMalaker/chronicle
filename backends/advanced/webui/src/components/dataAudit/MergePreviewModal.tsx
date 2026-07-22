import { useMemo, useState } from 'react'
import { AlertTriangle, GitMerge, Loader2 } from 'lucide-react'
import { AuditConversation, dataAuditApi } from '../../services/api'
import { formatDate, formatDuration } from './format'
import { Alert, Button, Modal } from '../ui'

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
    <Modal
      open
      onClose={onClose}
      title={`Merge ${ordered.length} conversations`}
      icon={<GitMerge className="h-5 w-5 text-blue-600" />}
      maxWidthClassName="max-w-xl max-h-[85vh] overflow-y-auto"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={confirmMerge}
            disabled={merging}
            icon={merging ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
          >
            Merge
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && (
          <Alert tone="danger" icon={<AlertTriangle className="h-4 w-4" />}>
            {error}
          </Alert>
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
    </Modal>
  )
}
