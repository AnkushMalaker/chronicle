import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Play, Pause, RefreshCw, Radio } from 'lucide-react'
import {
  annotationsApi,
  BACKEND_URL,
  BackgroundCandidate,
  dataAuditApi,
} from '../../services/api'
import { getStorageKey } from '../../utils/storage'
import { formatClock } from './format'

// Kept in sync with backend constants.NOISE_LABEL. Confirming a candidate posts
// this as a diarization annotation, which the backend auto-adds to the
// background bucket (the accumulation loop).
const NOISE_LABEL = 'Noise'

interface Props {
  conversationId: string
  onDecisionsChanged?: () => void
}

/**
 * "Potentially background" review: ranks a conversation's unidentified segments
 * by how likely they are background (max similarity to the confirmed-background
 * bucket + low SNR), and lets the user play each clip and confirm it as
 * background. Each confirmation grows the bucket, so ranking improves over time.
 */
export default function BackgroundReview({ conversationId, onDecisionsChanged }: Props) {
  const [candidates, setCandidates] = useState<BackgroundCandidate[]>([])
  const [bucketSize, setBucketSize] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [playingKey, setPlayingKey] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<Set<number>>(new Set())
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const token = () => localStorage.getItem(getStorageKey('token')) || ''
  const keyOf = (c: BackgroundCandidate) => `${c.segment_index}:${c.start}`

  const stopAudio = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlayingKey(null)
  }, [])

  useEffect(() => () => stopAudio(), [stopAudio])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await dataAuditApi.backgroundSuggest(conversationId, 15)
      setCandidates(res.data.candidates)
      setBucketSize(res.data.bucket_size)
    } catch {
      setError('Failed to load background candidates')
    } finally {
      setLoading(false)
    }
  }, [conversationId])

  const play = (c: BackgroundCandidate) => {
    const key = keyOf(c)
    if (playingKey === key) {
      stopAudio()
      return
    }
    stopAudio()
    const url =
      `${BACKEND_URL}/api/audio/chunks/${conversationId}` +
      `?start_time=${c.start.toFixed(2)}&end_time=${c.end.toFixed(2)}&format=wav&token=${token()}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlayingKey(key)
    audio.addEventListener('ended', () => stopAudio())
    audio.addEventListener('error', () => {
      setError('Playback failed')
      stopAudio()
    })
    audio.play().catch(() => stopAudio())
  }

  const confirmBackground = async (c: BackgroundCandidate) => {
    setConfirming((prev) => new Set(prev).add(c.segment_index))
    try {
      await annotationsApi.createDiarizationAnnotation({
        conversation_id: conversationId,
        segment_index: c.segment_index,
        original_speaker: '',
        corrected_speaker: NOISE_LABEL,
        segment_start_time: c.segment_start_time,
      })
      setCandidates((prev) => prev.filter((x) => keyOf(x) !== keyOf(c)))
      setBucketSize((n) => (n === null ? n : n + 1))
      onDecisionsChanged?.()
    } catch {
      setError('Failed to confirm background')
    } finally {
      setConfirming((prev) => {
        const next = new Set(prev)
        next.delete(c.segment_index)
        return next
      })
    }
  }

  return (
    <div className="rounded-md border border-gray-200 dark:border-gray-700 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-200">
          <Radio className="w-4 h-4 text-amber-500" />
          Potentially background
          {bucketSize !== null && (
            <span className="text-xs font-normal text-gray-400">
              bucket: {bucketSize} clip{bucketSize === 1 ? '' : 's'}
            </span>
          )}
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1 text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
        >
          {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          {candidates.length ? 'Refresh' : 'Find candidates'}
        </button>
      </div>

      {error && <div className="text-xs text-red-500 mb-2">{error}</div>}

      {!loading && bucketSize === 0 && candidates.length === 0 && (
        <div className="text-xs text-gray-400">
          The background bucket is empty. Confirm a few clips here (or mark
          Noise in triage) and matching will kick in.
        </div>
      )}

      {candidates.length > 0 && (
        <ul className="space-y-1">
          {candidates.map((c) => {
            const key = keyOf(c)
            const pct = Math.round(Math.max(0, Math.min(1, c.background_likelihood)) * 100)
            return (
              <li
                key={key}
                className="flex items-center gap-2 text-xs py-1 border-b border-gray-100 dark:border-gray-800 last:border-0"
              >
                <button
                  onClick={() => play(c)}
                  className="flex-shrink-0 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800"
                  title="Play clip"
                >
                  {playingKey === key ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                </button>
                <span className="flex-shrink-0 tabular-nums text-gray-400 w-24">
                  {formatClock(c.start)}–{formatClock(c.end)}
                </span>
                <div className="flex-shrink-0 w-16 h-1.5 rounded bg-gray-200 dark:bg-gray-700 overflow-hidden" title={`background likelihood ${pct}%`}>
                  <div className="h-full bg-amber-500" style={{ width: `${pct}%` }} />
                </div>
                <span className="flex-shrink-0 tabular-nums text-gray-400 w-28">
                  sim {c.bucket_similarity.toFixed(2)}
                  {c.snr_db !== null && ` · ${c.snr_db.toFixed(0)}dB`}
                </span>
                <span className="flex-1 truncate text-gray-500 dark:text-gray-400" title={c.text}>
                  {c.text || <em className="opacity-60">(non-speech)</em>}
                </span>
                <button
                  onClick={() => confirmBackground(c)}
                  disabled={confirming.has(c.segment_index)}
                  className="flex-shrink-0 px-2 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 hover:bg-amber-500/20 disabled:opacity-50"
                >
                  {confirming.has(c.segment_index) ? '…' : 'Background'}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
