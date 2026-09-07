import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Loader2, Scissors } from 'lucide-react'
import { AuditConversation, dataAuditApi } from '../../services/api'
import { formatDuration } from './format'
import { Alert, Button, Modal } from '../ui'

interface Props {
  conversations: AuditConversation[]
  // Minimum silence-gap length to split at (seconds) — taken from the active
  // "Silence gaps" filter so the bulk split matches what the user filtered for.
  minGapSeconds: number
  onClose: () => void
  onDone: (message: string) => void
}

type PreviewStatus = 'loading' | 'ready' | 'needs_analysis' | 'error'

interface Preview {
  conversationId: string
  title: string
  durationSeconds: number
  status: PreviewStatus
  // Split points (seconds) — one per detected gap, where speech resumes.
  splitPoints: number[]
  error?: string
}

const SPEECH_THRESHOLD = 0.5

export default function BulkSplitModal({ conversations, minGapSeconds, onClose, onDone }: Props) {
  const [previews, setPreviews] = useState<Preview[]>(() =>
    conversations.map((c) => ({
      conversationId: c.conversation_id,
      title: c.title || c.conversation_id.slice(0, 8),
      durationSeconds: c.duration_seconds,
      status: 'loading',
      splitPoints: [],
    }))
  )
  const [splitting, setSplitting] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // The precise (chunk-level) split points are recomputed per conversation —
  // the list filter only used the coarse cached speech_regions, so this is the
  // authoritative preview of what the split will actually do.
  const loadPreviews = useCallback(async () => {
    await Promise.all(
      conversations.map(async (c) => {
        try {
          const res = await dataAuditApi.getSilenceGaps(c.conversation_id, {
            speech_threshold: SPEECH_THRESHOLD,
            min_gap_seconds: minGapSeconds,
          })
          setPreviews((prev) =>
            prev.map((p) =>
              p.conversationId === c.conversation_id
                ? {
                    ...p,
                    status: res.data.needs_analysis ? 'needs_analysis' : 'ready',
                    splitPoints: res.data.gaps.map((g) => g.split_point_seconds),
                  }
                : p
            )
          )
        } catch (e: any) {
          setPreviews((prev) =>
            prev.map((p) =>
              p.conversationId === c.conversation_id
                ? {
                    ...p,
                    status: 'error',
                    error: e?.response?.data?.error || 'Failed to detect gaps',
                  }
                : p
            )
          )
        }
      })
    )
  }, [conversations, minGapSeconds])

  useEffect(() => {
    loadPreviews()
  }, [loadPreviews])

  const loadingPreviews = previews.some((p) => p.status === 'loading')
  const splittable = useMemo(
    () => previews.filter((p) => p.status === 'ready' && p.splitPoints.length > 0),
    [previews]
  )
  const totalNewParts = useMemo(
    () => splittable.reduce((sum, p) => sum + p.splitPoints.length + 1, 0),
    [splittable]
  )
  const skipped = previews.length - splittable.length

  const confirmSplit = async () => {
    if (splittable.length === 0) return
    setSplitting(true)
    setError(null)
    let done = 0
    let failed = 0
    let newParts = 0
    for (const p of splittable) {
      setProgress(`Splitting ${done + failed + 1}/${splittable.length}…`)
      try {
        const res = await dataAuditApi.split(p.conversationId, p.splitPoints)
        done++
        newParts += res.data.children.length
      } catch {
        failed++
      }
    }
    onDone(
      `Split ${done} conversation${done === 1 ? '' : 's'} into ${newParts} parts` +
        `${failed > 0 ? `; ${failed} failed` : ''}. Memory and title generation are queued.`
    )
    onClose()
  }

  return (
    <Modal
      open
      onClose={onClose}
      closeOnEscape={!splitting}
      closeOnBackdrop={!splitting}
      icon={<Scissors className="h-5 w-5 text-blue-600" />}
      title={
        <>
          Split at silence gaps
          <span className="block text-xs font-normal text-gray-500 dark:text-gray-400">
            {conversations.length} selected · splitting at gaps ≥ {formatDuration(minGapSeconds)}
          </span>
        </>
      }
      maxWidthClassName="max-w-2xl max-h-[85vh] overflow-y-auto"
      footer={
        <div className="flex items-center justify-between w-full">
          <p className="text-xs text-gray-400 max-w-sm">
            {progress ||
              'Each original is soft-deleted (recoverable from Archive). Transcripts are reassigned by time; memories and titles regenerate per part.'}
          </p>
          <div className="flex items-center space-x-2">
            <Button variant="secondary" onClick={onClose} disabled={splitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={confirmSplit}
              disabled={loadingPreviews || splittable.length === 0 || splitting}
              icon={splitting ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
            >
              Split {splittable.length} conversation{splittable.length === 1 ? '' : 's'}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-4">
        {/* Summary */}
        <div className="rounded-lg bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 p-3 text-sm text-gray-700 dark:text-gray-200">
          {loadingPreviews ? (
            <span className="flex items-center space-x-2 text-gray-500 dark:text-gray-400">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>Detecting gaps…</span>
            </span>
          ) : (
            <span>
              <strong>{splittable.length}</strong> conversation
              {splittable.length === 1 ? '' : 's'} → <strong>{totalNewParts}</strong> new parts
              {skipped > 0 && (
                <span className="text-gray-500 dark:text-gray-400">
                  {' '}
                  · {skipped} skipped (no qualifying gaps / not analyzed)
                </span>
              )}
            </span>
          )}
        </div>

        {error && (
          <Alert tone="danger" icon={<AlertTriangle className="h-4 w-4" />}>
            {error}
          </Alert>
        )}

        {/* Per-conversation breakdown */}
        <div className="space-y-1.5 max-h-72 overflow-y-auto">
          {previews.map((p) => {
            const parts = p.splitPoints.length + 1
            return (
              <div
                key={p.conversationId}
                className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 text-sm"
              >
                <div className="min-w-0 mr-3">
                  <p className="truncate text-gray-700 dark:text-gray-200">{p.title}</p>
                  <p className="text-xs text-gray-400">{formatDuration(p.durationSeconds)}</p>
                </div>
                <div className="flex-shrink-0 text-xs">
                  {p.status === 'loading' && (
                    <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
                  )}
                  {p.status === 'ready' && p.splitPoints.length > 0 && (
                    <span className="px-2 py-0.5 rounded bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300">
                      {p.splitPoints.length} gap{p.splitPoints.length === 1 ? '' : 's'} → {parts}{' '}
                      parts
                    </span>
                  )}
                  {p.status === 'ready' && p.splitPoints.length === 0 && (
                    <span className="text-gray-400">no qualifying gaps</span>
                  )}
                  {p.status === 'needs_analysis' && (
                    <span className="text-amber-600 dark:text-amber-400">not analyzed</span>
                  )}
                  {p.status === 'error' && (
                    <span className="text-red-500" title={p.error}>
                      error
                    </span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </Modal>
  )
}
