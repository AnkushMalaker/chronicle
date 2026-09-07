import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, Play, Pause, VolumeX } from 'lucide-react'
import {
  BACKEND_URL,
  BackgroundSuppressionCluster,
  BackgroundSuppressionSegment,
  BackgroundSuppressionsResponse,
  dataAuditApi,
} from '../services/api'
import { getStorageKey } from '../utils/storage'
import { IconButton, MetadataChip } from './ui'

interface Props {
  conversationId: string
  onChanged?: () => void
}

// Fragmented sources (a long video drifts across many small clusters) can
// produce dozens of rows; show the biggest first and let the user opt into
// the tail rather than flooding the page.
const CLUSTERS_SHOWN = 8

const ZONE_LABEL: Record<string, string> = {
  confident_background: 'background',
  unsure: 'unsure',
}

const STATUS_LABEL: Record<string, string> = {
  applied: 'marked background',
  shadow: 'would be marked',
  queued: 'needs review',
  restored: 'restored by you',
  confirmed: 'confirmed by you',
}

/**
 * "We marked these segments as background — review if you want."
 *
 * Discloses the background-suppression ledger for one conversation, grouped by
 * acoustic cluster (one media source ≈ one cluster). Restore puts the original
 * labels back and exempts the cluster; Confirm feeds exemplars to the bucket.
 * Shadow entries come from backfill and never touched the transcript.
 */
export default function BackgroundSuppressionCard({ conversationId, onChanged }: Props) {
  const [data, setData] = useState<BackgroundSuppressionsResponse | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [showAllClusters, setShowAllClusters] = useState(false)
  const [deciding, setDeciding] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [playingKey, setPlayingKey] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const token = () => localStorage.getItem(getStorageKey('token')) || ''

  const stopAudio = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlayingKey(null)
  }, [])

  useEffect(() => () => stopAudio(), [stopAudio])

  const load = useCallback(async () => {
    try {
      const res = await dataAuditApi.backgroundSuppressions(conversationId)
      setData(res.data)
    } catch {
      // Ledger endpoint failing should never break the conversation page.
      setData(null)
    }
  }, [conversationId])

  useEffect(() => {
    load()
  }, [load])

  if (!data || data.total === 0) return null

  const play = (segment: BackgroundSuppressionSegment) => {
    const key = `${segment.segment_start}`
    if (playingKey === key) {
      stopAudio()
      return
    }
    stopAudio()
    const url =
      `${BACKEND_URL}/api/audio/chunks/${conversationId}` +
      `?start_time=${segment.segment_start.toFixed(2)}&end_time=${segment.segment_end.toFixed(2)}` +
      `&format=wav&token=${token()}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlayingKey(key)
    audio.addEventListener('ended', () => stopAudio())
    audio.addEventListener('error', () => stopAudio())
    audio.play().catch(() => stopAudio())
  }

  const decide = async (cluster: BackgroundSuppressionCluster, decision: 'restore' | 'confirm') => {
    setDeciding(cluster.cluster_signature)
    setError(null)
    try {
      await dataAuditApi.backgroundSuppressionDecide(
        conversationId,
        cluster.cluster_signature,
        decision
      )
      await load()
      onChanged?.()
    } catch {
      setError('Could not save the decision — try again')
    } finally {
      setDeciding(null)
    }
  }

  const activeCounts = data.status_counts
  const marked = (activeCounts['applied'] || 0) + (activeCounts['shadow'] || 0)
  const queued = activeCounts['queued'] || 0
  const summaryParts: string[] = []
  if (marked) summaryParts.push(`${marked} marked background`)
  if (queued) summaryParts.push(`${queued} unsure`)

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50/50 dark:border-amber-900/50 dark:bg-amber-950/20">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-3 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-amber-600" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-amber-600" />
        )}
        <VolumeX className="h-4 w-4 shrink-0 text-amber-600" />
        <span className="text-sm font-medium text-gray-800 dark:text-gray-100">
          Background audio
        </span>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {summaryParts.join(' · ') || `${data.total} reviewed`} — review if you want
        </span>
      </button>
      {expanded && (
        <div className="space-y-3 border-t border-amber-200/70 px-4 py-3 dark:border-amber-900/40">
          {data.subject_override && (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              You marked this conversation's media as important — new segments are not
              auto-marked here.
            </p>
          )}
          {error && <p className="text-xs text-red-600">{error}</p>}
          {(showAllClusters ? data.clusters : data.clusters.slice(0, CLUSTERS_SHOWN)).map((cluster) => {
            const undecided = cluster.segments.filter(
              (s) => s.status !== 'restored' && s.status !== 'confirmed'
            )
            const settled = undecided.length === 0
            return (
              <div
                key={cluster.cluster_signature}
                className="rounded border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900"
              >
                <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                  <span>
                    {cluster.segments.length} segment{cluster.segments.length === 1 ? '' : 's'}
                  </span>
                  <span>· sim {cluster.max_background_similarity.toFixed(2)}</span>
                  {Object.entries(cluster.statuses).map(([status, count]) => (
                    <MetadataChip key={status}>
                      {count} {STATUS_LABEL[status] || status}
                    </MetadataChip>
                  ))}
                </div>
                <ul className="space-y-1.5">
                  {cluster.segments.slice(0, 5).map((segment) => (
                    <li
                      key={segment.segment_start}
                      className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-200"
                    >
                      <IconButton
                        onClick={() => play(segment)}
                        className="mt-0.5 shrink-0"
                        label="Play clip"
                      >
                        {playingKey === `${segment.segment_start}` ? (
                          <Pause className="h-3.5 w-3.5" />
                        ) : (
                          <Play className="h-3.5 w-3.5" />
                        )}
                      </IconButton>
                      <span className="text-xs text-gray-400">
                        {segment.segment_start.toFixed(0)}s
                      </span>
                      <span className="min-w-0 flex-1 truncate">
                        {segment.text || <em className="text-gray-400">no transcript</em>}
                      </span>
                      <span className="shrink-0 text-xs text-gray-400">
                        {ZONE_LABEL[segment.zone] || segment.zone}
                      </span>
                    </li>
                  ))}
                  {cluster.segments.length > 5 && (
                    <li className="text-xs text-gray-400">
                      … and {cluster.segments.length - 5} more
                    </li>
                  )}
                </ul>
                {!settled && (
                  <div className="mt-2 flex gap-2">
                    <button
                      disabled={deciding === cluster.cluster_signature}
                      onClick={() => decide(cluster, 'restore')}
                      className="rounded border border-gray-300 px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
                    >
                      Important speech — restore
                    </button>
                    <button
                      disabled={deciding === cluster.cluster_signature}
                      onClick={() => decide(cluster, 'confirm')}
                      className="rounded bg-gray-900 px-2.5 py-1 text-xs text-white disabled:opacity-50 dark:bg-gray-700 dark:text-gray-100"
                    >
                      Confirm background
                    </button>
                    {deciding === cluster.cluster_signature && (
                      <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
                    )}
                  </div>
                )}
              </div>
            )
          })}
          {!showAllClusters && data.clusters.length > CLUSTERS_SHOWN && (
            <button
              onClick={() => setShowAllClusters(true)}
              className="text-xs text-gray-500 underline hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            >
              Show {data.clusters.length - CLUSTERS_SHOWN} more clusters
            </button>
          )}
        </div>
      )}
    </div>
  )
}
