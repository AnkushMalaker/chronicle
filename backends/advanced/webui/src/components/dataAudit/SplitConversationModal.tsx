import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Loader2, Scissors, X } from 'lucide-react'
import { SilenceGap, dataAuditApi } from '../../services/api'
import { useJobPolling } from '../../hooks/useJobPolling'
import { formatClock, formatDuration } from './format'
import PreviewStrip from './PreviewStrip'

// Minimal shape so both the Data Audit table rows and the conversation
// detail page can open this modal.
export interface SplitTarget {
  conversation_id: string
  title: string | null
  duration_seconds: number
}

interface Props {
  conversation: SplitTarget
  onClose: () => void
  onDone: (message: string) => void
}

export default function SplitConversationModal({ conversation, onClose, onDone }: Props) {
  const [gapMinutes, setGapMinutes] = useState(15)
  const [speechThreshold, setSpeechThreshold] = useState(0.5)
  const [gaps, setGaps] = useState<SilenceGap[]>([])
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [duration, setDuration] = useState(0)
  const [needsAnalysis, setNeedsAnalysis] = useState(false)
  const [loading, setLoading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [splitting, setSplitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { pollJob } = useJobPolling()

  const loadGaps = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await dataAuditApi.getSilenceGaps(conversation.conversation_id, {
        speech_threshold: speechThreshold,
        min_gap_seconds: gapMinutes * 60,
      })
      setNeedsAnalysis(res.data.needs_analysis)
      setDuration(res.data.duration_seconds)
      setGaps(res.data.gaps)
      // All suggested split points start checked.
      setChecked(new Set(res.data.gaps.map((g) => g.split_point_seconds)))
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load silence gaps')
    } finally {
      setLoading(false)
    }
  }, [conversation.conversation_id, speechThreshold, gapMinutes])

  // Initial load + debounced refetch when controls change.
  useEffect(() => {
    const t = setTimeout(loadGaps, 300)
    return () => clearTimeout(t)
  }, [loadGaps])

  const runAnalysis = async () => {
    setAnalyzing(true)
    setError(null)
    try {
      const res = await dataAuditApi.analyze([conversation.conversation_id], true)
      const status = await pollJob(res.data.job_id)
      if (status === 'failed') {
        setError('Audio analysis failed — check the Queue page for details.')
      } else {
        await loadGaps()
      }
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to start analysis')
    } finally {
      setAnalyzing(false)
    }
  }

  const toggleGap = (splitPoint: number) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(splitPoint)) next.delete(splitPoint)
      else next.add(splitPoint)
      return next
    })
  }

  // Live preview of resulting children from the checked split points.
  const previewParts = useMemo(() => {
    const points = Array.from(checked).sort((a, b) => a - b)
    const edges = [0, ...points, duration]
    const parts: { start: number; end: number }[] = []
    for (let i = 0; i < edges.length - 1; i++) {
      parts.push({ start: edges[i], end: edges[i + 1] })
    }
    return parts
  }, [checked, duration])

  const confirmSplit = async () => {
    const points = Array.from(checked).sort((a, b) => a - b)
    if (points.length === 0) return
    setSplitting(true)
    setError(null)
    try {
      const res = await dataAuditApi.split(conversation.conversation_id, points)
      onDone(
        `Split "${conversation.title || conversation.conversation_id.slice(0, 8)}" into ` +
          `${res.data.children.length} conversations. Memory and title generation are queued.`
      )
      onClose()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to split conversation')
      setSplitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-2xl max-h-[85vh] overflow-y-auto rounded-xl bg-white dark:bg-gray-800 shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center space-x-2">
            <Scissors className="h-5 w-5 text-blue-600" />
            <div>
              <h3 className="font-semibold text-gray-900 dark:text-gray-100">Split conversation</h3>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {conversation.title || conversation.conversation_id.slice(0, 8)} ·{' '}
                {formatDuration(conversation.duration_seconds)}
              </p>
            </div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="px-6 py-4 space-y-4">
          {/* Controls */}
          <div className="flex flex-wrap items-end gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
                Min. silence gap (minutes)
              </label>
              <input
                type="number" min={1} step={1}
                value={gapMinutes}
                onChange={(e) => setGapMinutes(Math.max(1, Number(e.target.value)))}
                className="w-28 mt-1 px-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-sm text-gray-700 dark:text-gray-200"
              />
            </div>
            <div className="min-w-44">
              <label className="flex justify-between text-xs font-medium text-gray-700 dark:text-gray-200">
                <span>VAD threshold</span>
                <span className="text-gray-500 dark:text-gray-400">{speechThreshold.toFixed(2)}</span>
              </label>
              <input
                type="range" min={0.1} max={0.9} step={0.05}
                value={speechThreshold}
                onChange={(e) => setSpeechThreshold(Number(e.target.value))}
                className="w-full mt-1"
              />
            </div>
            {loading && <Loader2 className="h-4 w-4 animate-spin text-gray-400 mb-2" />}
          </div>

          {error && (
            <div className="flex items-center space-x-2 text-sm px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-200">
              <AlertTriangle className="h-4 w-4" />
              <span>{error}</span>
            </div>
          )}

          {/* Needs analysis */}
          {needsAnalysis && !loading && (
            <div className="rounded-lg border border-yellow-300 dark:border-yellow-700 bg-yellow-50 dark:bg-yellow-900/20 p-4 space-y-2">
              <p className="text-sm text-yellow-800 dark:text-yellow-200">
                This conversation's audio hasn't been VAD-analyzed yet. Run analysis to locate
                silence gaps (this decodes the audio and can take a few minutes for long recordings).
              </p>
              <button
                onClick={runAnalysis}
                disabled={analyzing}
                className="flex items-center space-x-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
              >
                {analyzing && <Loader2 className="h-4 w-4 animate-spin" />}
                <span>{analyzing ? 'Analyzing audio…' : 'Analyze audio'}</span>
              </button>
            </div>
          )}

          {/* Speech timeline: blue = speech, amber = detected gaps, red = chosen split points */}
          {!needsAnalysis && !loading && duration > 0 && (
            <PreviewStrip
              conversationId={conversation.conversation_id}
              durationSeconds={duration}
              overlays={gaps.map((g) => ({ start: g.start_seconds, end: g.end_seconds }))}
              markers={Array.from(checked)}
            />
          )}

          {/* Gap list */}
          {!needsAnalysis && !loading && (
            <>
              {gaps.length === 0 ? (
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  No silence gaps of {gapMinutes}+ minutes found. Lower the gap length or the VAD
                  threshold to find more candidates.
                </p>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs font-medium text-gray-700 dark:text-gray-200">
                    Detected gaps — checked gaps become split points (split happens where speech resumes):
                  </p>
                  {gaps.map((g) => (
                    <label
                      key={g.split_point_seconds}
                      className="flex items-center space-x-3 px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/40"
                    >
                      <input
                        type="checkbox"
                        checked={checked.has(g.split_point_seconds)}
                        onChange={() => toggleGap(g.split_point_seconds)}
                      />
                      <span className="font-mono">
                        {formatClock(g.start_seconds)} → {formatClock(g.end_seconds)}
                      </span>
                      <span className="text-gray-400">·</span>
                      <span>{formatDuration(g.duration_seconds)} of silence</span>
                    </label>
                  ))}
                </div>
              )}

              {/* Preview */}
              {checked.size > 0 && (
                <div className="rounded-lg bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 p-3 space-y-1">
                  <p className="text-xs font-medium text-gray-700 dark:text-gray-200">
                    Result: {previewParts.length} conversations
                  </p>
                  {previewParts.map((p, i) => (
                    <p key={i} className="text-xs font-mono text-gray-500 dark:text-gray-400">
                      Part {i + 1}/{previewParts.length} · {formatClock(p.start)}–{formatClock(p.end)} ·{' '}
                      {formatDuration(p.end - p.start)}
                    </p>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <p className="text-xs text-gray-400 max-w-sm">
            The original is soft-deleted (recoverable from Archive). Transcript segments are
            reassigned by time; memories and titles are regenerated per part.
          </p>
          <div className="flex items-center space-x-2">
            <button
              onClick={onClose}
              className="px-4 py-2 rounded-lg text-sm font-medium border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={confirmSplit}
              disabled={checked.size === 0 || splitting || needsAnalysis}
              className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {splitting && <Loader2 className="h-4 w-4 animate-spin" />}
              <span>Split into {Math.max(previewParts.length, 2)} parts</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
